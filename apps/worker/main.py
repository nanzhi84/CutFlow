from __future__ import annotations

from packages.core.storage import Repository
from packages.production.pipeline import build_digital_human_workflow


def main() -> None:
    workflow = build_digital_human_workflow(Repository())
    node_count = len(workflow.template.nodes)
    print(f"Cutagent worker ready: {workflow.template.workflow_template_id}@{workflow.template.version}")
    print(f"Registered activity contracts: {node_count}")


if __name__ == "__main__":
    main()
