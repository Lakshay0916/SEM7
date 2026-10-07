#!/usr/bin/env bash
# Delete everything provision.sh created (stops all charges). Irreversible:
# the database and workflow history on the server are lost.
#
#   AWS_PROFILE=sem7 deploy/teardown.sh
set -euo pipefail

export AWS_REGION="${AWS_REGION:-ap-south-1}"
NAME=sem7

for alloc in $(aws ec2 describe-addresses --filters Name=tag:Project,Values=$NAME --query 'Addresses[].AllocationId' --output text); do
  assoc="$(aws ec2 describe-addresses --allocation-ids "$alloc" --query 'Addresses[0].AssociationId' --output text)"
  [[ "$assoc" != "None" ]] && aws ec2 disassociate-address --association-id "$assoc"
  aws ec2 release-address --allocation-id "$alloc" && echo "released elastic IP $alloc"
done

ids="$(aws ec2 describe-instances --filters Name=tag:Project,Values=$NAME \
  Name=instance-state-name,Values=pending,running,stopping,stopped --query 'Reservations[].Instances[].InstanceId' --output text)"
if [[ -n "$ids" ]]; then
  aws ec2 terminate-instances --instance-ids $ids >/dev/null
  echo "terminating $ids ..."
  aws ec2 wait instance-terminated --instance-ids $ids
fi

sg="$(aws ec2 describe-security-groups --filters Name=group-name,Values=$NAME-sg --query 'SecurityGroups[0].GroupId' --output text)"
[[ "$sg" != "None" ]] && aws ec2 delete-security-group --group-id "$sg" && echo "deleted security group $sg"

aws ec2 delete-key-pair --key-name "$NAME-key" >/dev/null && echo "deleted key pair $NAME-key"
echo "done - nothing billable left from this project"
