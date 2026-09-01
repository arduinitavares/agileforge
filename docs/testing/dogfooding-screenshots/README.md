# AgileForge Dogfooding UI/UX Session Chronicle

This directory contains the complete sequence of 29 screenshots captured during the **String Calculator Lab** end-to-end dogfooding campaign on August 31, 2026.

This chronicle documents what each image represents in the AgileForge lifecycle, highlighting key UI controls, state transitions, and specific UX observations to support the upcoming UI/UX refactoring.

---

## Screenshot Index & Workflow Flow

| # | Filename | Timestamp | Lifecycle Phase | Description & Key UI Elements |
| :--- | :--- | :--- | :--- | :--- |
| 01 | `media_1788200925065.png` | 15:29 | Phase 1: Vision | Project Vision accepted state showing Accepted Vision card and transition into Product Goal interview. |
| 02 | `media_1788201064243.png` | 15:31 | Phase 2: Goal | Active Product Goal showing accepted vision context and valuable outcome statement. |
| 03 | `media_1788201417223.png` | 15:37 | Phase 3: Specification | Specification registration with source path `docs/spec/string-calculator-first-release.md` and preparation capability `grill-with-docs`. |
| 04 | `media_1788201600610.png` | 15:40 | Phase 3: Spec Review | Accepted Specification candidate card showing immutable sha256 hash and ADR lineage. |
| 05 | `media_1788201737375.png` | 15:42 | Phase 4: Backlog Gen | Delivery section showing green action button: `Generate Backlog from accepted Specification`. |
| 06 | `media_1788201859955.png` | 15:47 | Phase 4: Backlog Review | Backlog Candidate modal rendering 7 PBIs with rank, story points, and INVEST criteria. |
| 07 | `media_1788202027386.png` | 15:47 | Phase 4: Backlog Detail | Detailed view of PBI-000001 through PBI-000003 with acceptance criteria and spec linkage. |
| 08 | `media_1788202294903.png` | 15:53 | Phase 4: Backlog Accept | Backlog review dialog prompting for human rationale to accept Backlog candidate into durable state. |
| 09 | `media_1788203286642.png` | 16:08 | Phase 5: Roadmap Gen | Delivery section showing action button: `Generate Roadmap from accepted Backlog`. |
| 10 | `media_1788203349525.png` | 16:09 | Phase 5: Roadmap Review | Roadmap Candidate modal displaying 5 dependency-safe milestones across the 7 PBIs. |
| 11 | `media_1788203374301.png` | 16:09 | Phase 5: Roadmap Milestone | Expanded milestone detail showing prerequisite ordering and release boundaries. |
| 12 | `media_1788203578466.png` | 16:13 | Phase 5: Roadmap Accept | Roadmap review acceptance dialog capturing operator rationale. |
| 13 | `media_1788203728951.png` | 16:16 | Phase 6: Story Gen | Progressive story generation prompt for `PBI-000001` (Public Python summation operation). |
| 14 | `media_1788203741489.png` | 16:16 | Phase 6: Story Review | User Story review modal for `US-0001` (PBI-000001) showing 3 story points and paired tasks. |
| 15 | `media_1788203762458.png` | 16:16 | Phase 6: Story Accept | Story acceptance rationale prompt for PBI-000001. |
| 16 | `media_1788204001908.png` | 16:21 | Phase 6: Story Gen PBI-2 | Story generation action for `PBI-000002` (Supported Number List language). |
| 17 | `media_1788204017746.png` | 16:21 | Phase 6: Story Review PBI-2| User Story review modal for `US-0001` (PBI-000002) showing 2 story points. |
| 18 | `media_1788204050773.png` | 16:21 | Phase 6: Story Accept PBI-2| Story acceptance dialog for PBI-000002. |
| 19 | `media_1788204368036.png` | 16:26 | Phase 6: Story Gen PBI-3 | Story generation action for `PBI-000003` (Numeric spelling & zero semantics). |
| 20 | `media_1788204566004.png` | 16:29 | Phase 6: Story Review PBI-3| User Story review modal for `US-0001` (PBI-000003) showing 3 story points. |
| 21 | `media_1788204834201.png` | 16:34 | Phase 6: Story Accept PBI-3| Story acceptance dialog for PBI-000003. |
| 22 | `media_1788206365483.png` | 17:00 | Phase 6: Sprint Selection | Story readiness pool showing 3 accepted stories selected for Sprint 1 (8 pts total). |
| 23 | `media_1788206410686.png` | 17:00 | Phase 6: Confirm Deps | Dependency confirmation state unlocking Phase 7 (Sprint Planning). |
| 24 | `media_1788206925761.png` | 17:09 | Phase 7: Sprint Planning | Sprint planning capacity input (`max_story_points`) and candidate story selection. |
| 25 | `media_1788206939583.png` | 17:09 | Phase 7: Sprint Plan Review | Sprint Plan review modal showing Sprint 1 Goal, 3 selected stories, and 6 paired tasks. |
| 26 | `media_1788206965566.png` | 17:09 | Phase 7: Sprint Start | Sprint acceptance rationale and `Start Sprint` action. |
| 27 | `media_1788213073550.png` | 18:52 | Phase 8: Sprint 1 Done | Top lifecycle status bar showing `Sprint: Complete`, `Review: Waiting (Sprint review required)`. |
| 28 | `media_1788213125776.png` | 18:52 | Phase 8: Delivery State | Active Sprint delivery section with all 6 tasks completed ("No execution action is currently available"). |
| 29 | `media_1788216568198.png` | 19:54 | Phase 9: Post-Sprint State | Post-Sprint 1 triage state highlighting UI gap: completed stories lingering in selection pool (Issue #232). |

---

## Detailed UI/UX Observations & Refactoring Recommendations

### 1. Sprint Planning Capacity Guidance (Linked to [Issue #231](https://github.com/arduinitavares/agileforge/issues/231))
- **Screenshots**: `media_1788206925761.png`, `media_1788206939583.png`
- **Observation**: When entering `Maximum story points`, if the user enters a number lower than the selected candidate total (e.g. 6 points when 8 points are selected), the UI should provide an interactive recommendation showing which candidate stories best fit within capacity.

### 2. Completed Story Filtering in Sprint Selection (Linked to [Issue #232](https://github.com/arduinitavares/agileforge/issues/232))
- **Screenshot**: `media_1788216568198.png`
- **Observation**: After Sprint 1 completed and closed, Stories 1, 2, and 3 continued to render with `Selected for Sprint`, `Remove from Sprint selection`, and `Defer` buttons, along with `Correct Stories for PBI-xxx` action cards.
- **Recommendation**: Filter completed stories from previous sprints into a read-only "Completed Stories" archive section, and only show "Correct Stories" if post-sprint triage explicitly recorded story-level impact.

### 3. Task Execution & Review Visibility
- **Screenshots**: `media_1788213073550.png`, `media_1788213125776.png`
- **Observation**: When all tasks in an active sprint are completed, the UI displays "No execution action is currently available" and marks the top Review tab as "Waiting". Adding an explicit "Complete Sprint Review" button directly in the delivery section will provide clearer guidance for operators.
