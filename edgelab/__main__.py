"""Run a short synthetic example without starting any service."""

import argparse
import json
from uuid import uuid4

from .contracts import FixtureApproval, Status
from .controller import ResearchRun
from .fixtures import Fixture, METHOD, SCENARIOS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", choices=SCENARIOS, default="conflict")
    parser.add_argument("--approve-fixture", action="store_true",
                        help="Explicitly approve only this in-process synthetic fixture")
    parser.add_argument("--without-method", action="store_true",
                        help="Show how missing method evidence stops the demo before approval")
    args = parser.parse_args()
    run = ResearchRun(f"demo-{uuid4().hex}", Fixture(args.fixture), None if args.without_method else METHOD)
    if args.approve_fixture and run.status == Status.AWAITING_APPROVAL:
        run.approve(FixtureApproval(run.binding("demo-approval")))
        while run.status == Status.READY:
            run.execute(run.propose_next(f"action-{run.version}"))
            run.review_milestone()
    if run.status == Status.FINAL_REVIEW:
        run.finalize()
    print(json.dumps(run.packet(), indent=2))


if __name__ == "__main__":
    main()
