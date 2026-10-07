#!/usr/bin/env bash
# Create the EC2 host with the AWS CLI (no Terraform). Idempotent: re-running reuses
# what already exists. Prints the public IP at the end.
#
#   AWS_PROFILE=sem7 deploy/provision.sh
#
# Creates (all tagged Project=sem7):
#   - key pair "sem7-key" from your ~/.ssh/id_ed25519.pub
#   - security group "sem7-sg": SSH from your current IP only, HTTP/HTTPS from anywhere
#   - one t3.small Ubuntu 24.04 instance, 20 GB gp3 disk, Docker installed on first boot
#   - an Elastic IP, so the address survives stop/start
set -euo pipefail

export AWS_REGION="${AWS_REGION:-ap-south-1}"
NAME=sem7
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.small}"
PUBKEY="${PUBKEY:-$HOME/.ssh/id_ed25519.pub}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TAGS="ResourceType=%s,Tags=[{Key=Name,Value=$NAME},{Key=Project,Value=$NAME}]"

MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
VPC_ID="$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
AMI_ID="$(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query Parameter.Value --output text)"
echo "region=$AWS_REGION vpc=$VPC_ID ami=$AMI_ID my_ip=$MY_IP"

# --- key pair
if ! aws ec2 describe-key-pairs --key-names "$NAME-key" >/dev/null 2>&1; then
  aws ec2 import-key-pair --key-name "$NAME-key" --public-key-material "fileb://$PUBKEY" \
    --tag-specifications "$(printf "$TAGS" key-pair)" >/dev/null
  echo "imported key pair $NAME-key"
fi

# --- security group
SG_ID="$(aws ec2 describe-security-groups --filters Name=group-name,Values=$NAME-sg Name=vpc-id,Values=$VPC_ID \
  --query 'SecurityGroups[0].GroupId' --output text)"
if [[ "$SG_ID" == "None" ]]; then
  SG_ID="$(aws ec2 create-security-group --group-name "$NAME-sg" --vpc-id "$VPC_ID" \
    --description "SEM7: SSH from admin IP, HTTP/HTTPS public" \
    --tag-specifications "$(printf "$TAGS" security-group)" --query GroupId --output text)"
  aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --ip-permissions \
    "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0}]" \
    "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0}]" >/dev/null
  echo "created security group $SG_ID"
fi
# SSH only from the current admin IP (adds a rule if your IP changed).
aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 22 --cidr "$MY_IP/32" \
  >/dev/null 2>&1 && echo "allowed SSH from $MY_IP" || true

# --- instance
INSTANCE_ID="$(aws ec2 describe-instances \
  --filters Name=tag:Project,Values=$NAME Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)"
if [[ "$INSTANCE_ID" == "None" ]]; then
  INSTANCE_ID="$(aws ec2 run-instances --image-id "$AMI_ID" --instance-type "$INSTANCE_TYPE" \
    --key-name "$NAME-key" --security-group-ids "$SG_ID" \
    --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=20,VolumeType=gp3,DeleteOnTermination=true}' \
    --metadata-options HttpTokens=required \
    --user-data "file://$HERE/cloud-init.sh" \
    --tag-specifications "$(printf "$TAGS" instance)" "$(printf "$TAGS" volume)" \
    --query 'Instances[0].InstanceId' --output text)"
  echo "launched $INSTANCE_ID"
fi
aws ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null 2>&1 || true
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"

# --- elastic IP
ALLOC_ID="$(aws ec2 describe-addresses --filters Name=tag:Project,Values=$NAME \
  --query 'Addresses[0].AllocationId' --output text)"
if [[ "$ALLOC_ID" == "None" ]]; then
  ALLOC_ID="$(aws ec2 allocate-address --domain vpc \
    --tag-specifications "$(printf "$TAGS" elastic-ip)" --query AllocationId --output text)"
fi
aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID" >/dev/null
PUBLIC_IP="$(aws ec2 describe-addresses --allocation-ids "$ALLOC_ID" --query 'Addresses[0].PublicIp' --output text)"

echo "instance=$INSTANCE_ID ip=$PUBLIC_IP"
echo "next: deploy/deploy.sh $PUBLIC_IP"
