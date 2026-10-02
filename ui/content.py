"""Plain-language explanations shown in the UI (kept in one place so wording stays consistent)."""

# Implemented steps of a pipeline run, in order.
STEPS = {
    "sandbox": {
        "title": "Developer commit in a sandbox repository",
        "what": (
            "The demo project is copied into a brand-new git repository. Its healthy code is committed to "
            "`main`, then the scenario's change is committed on a feature branch - exactly what a developer "
            "pushing a branch would produce."
        ),
        "why": (
            "Every run is isolated and reproducible: a real git history to analyse, and no risk to any real "
            "codebase. `main` always stays healthy so the AI has a known-good baseline."
        ),
    },
    "diff": {
        "title": "Change analysis (git diff)",
        "what": (
            "The feature branch is compared with `main`. Each changed file is classified (source, test, "
            "dependency, config) and changed line numbers are mapped onto Python's syntax tree to find "
            "which functions were modified."
        ),
        "why": (
            "Knowing precisely what changed is the input to the risk model now, and to the Investigation "
            "Agent later (a failure in `test_add` right after `add()` changed is strong evidence)."
        ),
    },
    "risk": {
        "title": "ML risk prediction",
        "what": (
            "The change is turned into 27 numeric features (size, files touched, tests changed, dependencies, "
            "...). A model trained on 3,625 real commits from 9 open-source projects estimates how likely "
            "this kind of change is to break CI."
        ),
        "why": (
            "An early warning before tests run, useful for prioritising review. It is a statistical signal, "
            "not proof: it cannot see the bug itself, and the diff-only model used here is modest "
            "(test ROC-AUC 0.60)."
        ),
    },
    "tests": {
        "title": "CI test run (real execution)",
        "what": (
            "`pytest` is actually executed in the sandbox with a time limit. Results are read from pytest's "
            "machine-readable JUnit report, not guessed from text."
        ),
        "why": (
            "Tests are the ground truth. The verdict is PASS only if pytest exited 0, at least one test ran, "
            "and nothing failed; a timeout, crash or empty run can never be reported as PASS."
        ),
    },
    "failure": {
        "title": "Failure evidence packaged",
        "what": (
            "When tests fail, the evidence is bundled into a typed `FailureEvent`: commit, branch, failing "
            "tests, error messages, logs and the diff."
        ),
        "why": (
            "This structured evidence is what the Investigation Agent will receive. Agents work from facts "
            "captured here, so they cannot invent logs or test results."
        ),
    },
    "record": {
        "title": "Workflow recorded",
        "what": (
            "Each stage moved the workflow through a state machine (RECEIVED → RISK_ANALYZED → TESTING → "
            "PASSED/FAILED) and every output was saved to SQLite with a timestamp."
        ),
        "why": (
            "Full traceability: anyone can audit commit → risk → tests → failure → (later) fix → PR. The "
            "state machine also makes illegal jumps, like repairing without approval, impossible."
        ),
    },
}

# Stages not built yet, shown so the user sees where the pipeline is going.
UPCOMING = [
    ("Investigation Agent", 5, "Reads the FailureEvent and summarises what failed, separating evidence from guesses."),
    ("RAG knowledge retrieval", 6, "Searches the project's code, tests and docs for the context relevant to the failure."),
    ("Root Cause Agent", 7, "Explains why the failure happened, with evidence and a confidence score."),
    ("Solution Planner", 7, "Proposes exact code changes, tests to run, risks and a rollback plan."),
    ("Human approval", 8, "You review the AI debugging report and approve or reject the fix. Nothing changes without this."),
    ("Code Repair Agent", 9, "Applies only the approved change, on a separate `ai/repair/<id>` branch - never on main."),
    ("Testing Agent + Reflection", 10, "Re-runs the real tests. If they fail, the system re-investigates (max 3 attempts)."),
    ("Pull Request", 12, "Opens a PR with the problem, root cause, fix and test evidence. Never auto-merged."),
]

# Top-of-page progress bar labels: (step id or None, label, phase)
PROGRESS = [
    ("sandbox", "Commit", 2),
    ("diff", "Diff", 2),
    ("risk", "Risk", 3),
    ("tests", "Tests", 2),
    (None, "Investigate", 5),
    (None, "RAG", 6),
    (None, "Root cause", 7),
    (None, "Plan", 7),
    (None, "Approval", 8),
    (None, "Repair", 9),
    (None, "Re-test", 10),
    (None, "PR", 12),
]

GLOSSARY = {
    "ROC-AUC": "Probability that a randomly chosen failing commit gets a higher risk score than a randomly "
               "chosen passing one. 0.5 = coin flip, 1.0 = perfect ranking.",
    "PR-AUC": "Average precision across all thresholds. Better than ROC-AUC when failures are rare; a model "
              "that guesses blindly scores the failure rate (~0.12 here).",
    "Precision": "Of the commits the model flagged as risky, the share that really failed.",
    "Recall": "Of the commits that really failed, the share the model flagged.",
    "F1": "Harmonic mean of precision and recall - one number balancing both.",
    "Confusion matrix (TN/FP/FN/TP)": "Counts of correct passes, false alarms, missed failures and caught failures "
                                      "at the chosen threshold.",
    "Baseline": "A trivial rule (e.g. 'always predict pass', 'the previous commit failed'). The ML model is only "
                "useful if it beats these.",
    "Temporal split": "Each repo's commits are ordered by time: the model learns from older commits and is tested "
                      "on newer ones, like predicting the future from the past.",
    "Leave-one-repo-out": "Train on 8 projects, test on the 9th it has never seen - measures whether the model "
                          "generalises to a new codebase.",
    "Permutation importance": "How much validation PR-AUC drops when one feature's values are shuffled - a "
                              "model-agnostic measure of how much the model relies on it.",
}
