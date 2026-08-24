# Technical Specification: HashbrownVision Phase 1

## product vision

For food manufacturing process and quality engineers responsible for fried hashbrown production lines who need to reliably detect and quantify critical visual defects when 3D inspection is unavailable and are frustrated by manual inspection and brittle ad-hoc tools, HashbrownVision Phase 1 is a Python-based offline, 2D-only machine-vision proof-of-concept pipeline that provides engineering-grade evidence that 2D color imaging alone can support reliable quality assessment and de-risk investment in a future 2D+3D inspection system. The system ingests existing conveyor-belt images, deterministically segments individual product instances, applies 2D defect detection, computes interpretable per-product metrics, and exports fully traceable, image- and instance-linked CSV outputs, while explicitly excluding real-time operation, PLC integration, dashboards, cloud services, and AI-heavy black-box components.

## Phase 1 Scope and Guardrails

- Strictly offline, batch-oriented execution
- Deterministic, reproducible processing
- 2D-only algorithms with no 3D assumptions
- CPU-only execution on Windows 10/11
- Python 3.10 baseline
- File-based inputs and outputs only
- No PLC, encoder, cloud, dashboard, or real-time dependencies

## Core Phase 1 Themes (Now)

### Offline Dataset Ingestion & Organization

- Ingest existing 2D color images from plant storage without changing camera setups
- Parse existing folder and file naming conventions to infer line, product, recipe, batch/lot, and timestamps
- Define batch-oriented datasets by time range, line, and recipe
- Generate lightweight, reproducible dataset manifests (CSV or text)
- Ensure fully offline, deterministic ingestion

### 2D product segmentation & Instance Extraction

- Deterministically segment and isolate individual product instances from conveyor-belt images containing multiple products
- Use strictly 2D, non-AI methods (color cues, morphology, connected components, shape heuristics)
- Support multiple product shapes and SKUs within the same image
- Produce per-instance masks or bounding boxes with stable instance IDs
- Maintain full traceability to source image, timestamp, inferred metadata, and segmentation parameters
- Provide file-based per-instance data structures for downstream processing

### 2D Color-Based Defect Detection Algorithms

- Detect blue foreign bodies on segmented product instances as the primary Phase 1 anchor
- Operate strictly on per-product instance data, not full-frame images
- Allow per-recipe tuning of thresholds, ROIs, color ranges, and blob size
- Maintain deterministic, explainable behavior with no AI or 3D components
- Run fully offline in batch mode via CLI or notebooks

### Quantitative per-product & Per-Batch Quality Metrics

- Compute interpretable per-product metrics (e.g., defect area %, foreign body count, severity scores)
- Aggregate metrics to per-batch and per-run statistics
- Apply configurable thresholds to derive pass/fail or risk flags
- Maintain linkage from metrics to product instance, segmentation output, detection results, and source image

### Traceable CSV Export & Data Lineage

- Export per-product instance metrics to stable, documented CSV schemas
- Include image path, timestamp, product/recipe ID, batch/lot ID (when available), instance ID, and configuration version
- Optionally export per-batch summary CSVs with links to instance-level data
- Guarantee bit-for-bit repeatability given identical inputs and configurations
- Store all outputs locally with no external dependencies

## Phase 1 Stretch Themes (Next)

### Text-File–Based Per-Recipe Configuration & Threshold System

- Human-readable configuration files defining segmentation, detection, and metric parameters
- Clear separation of global defaults and per-recipe overrides
- Validation with informative error messages
- Versioning conventions tied to experimental outputs

### Minimal Validation & Evaluation Workflow

- Scripts or notebooks for controlled experiments on labeled datasets
- Performance metrics (precision, recall, confusion matrices) focused on blue foreign-body detection
- Comparison across recipes and parameter sets
- Documentation of limitations and Phase 2 recommendations

## Internal Phase 1 Milestones

- **M1:** Offline Dataset Ingestion & Organization (images + manifests, no segmentation)
- **M2:** 2D product segmentation & Instance Extraction (per-product instances, no detectors)
- **M3:** Blue Foreign-Body Detection on Segmented Instances
- **M4:** Quantitative per-product & Per-Batch Metrics
- **M5:** Traceable CSV Export & Data Lineage
