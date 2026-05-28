# Scrum Theory For As-Built Assessment

The As-Built Assessment Agent runs before Product Backlog generation for
brownfield projects. Its output is advisory context, not a Product Backlog and
not a Sprint Backlog.

In Scrum terms, the Product Owner remains accountable for deciding what enters
the Product Backlog and how that backlog is ordered. The assessment can identify
that a behavior appears observed, contradicted, not observed, or unclear, but it
does not create persisted backlog rows and does not select Sprint work.

An `observed` assessment should suppress duplicate implementation candidates.
If the repository already appears to satisfy an accepted obligation, the
backlog should not treat that obligation as greenfield work unless the Product
Owner explicitly asks for replacement or redesign.

An `unclear` assessment routes to discovery or Product Owner review. The system
should not guess implementation work from ambiguous evidence because that would
convert uncertainty into scope without Product Owner ordering.

The Sprint Backlog is still created later through AgileForge Sprint Planning.
This agent only improves the information available before backlog candidates
are drafted.
