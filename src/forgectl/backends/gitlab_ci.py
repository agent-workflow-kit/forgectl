"""GitLab `ci` (#17): the command over the shared head-pipeline read.

The read itself is ``GitLabBackend._ci`` (statuses of ``head_pipeline`` from the pipeline's own
project, the pipeline's status as a `(pipeline)` check, child pipelines through `/bridges`); `show`,
`snapshot` and `ci` all use it, so the three agree. ``ci`` reads the MR once as `show` does and
returns that read's CI object.

Merged-results and merge-train pipelines (TODO(#31), user decision pending). Such a pipeline runs
on the merge ref, so ``head_pipeline.sha`` is not the MR's head: ``for_current_head`` is false and
``ci.state`` is `pending`, never `passing`. This fails closed and is the documented behavior until
a live host shows a better rule; the default must not change without that decision.
"""

from __future__ import annotations

from collections.abc import Callable

from .. import model as m
from ..identifiers import PrRef


def ci(ref: PrRef, get_pr: Callable[[PrRef], m.PrRead]) -> m.CiResult:
    """The CI object of the MR's head, from the same read `show` makes."""
    read = get_pr(ref)
    return m.CiResult(pr=read.pr.pr, head_sha=read.pr.head.sha, ci=read.ci)
