# ruff: noqa: E501
"""Quick test to verify query_features_for_stories returns validated Pydantic schema with theme/epic."""

from tools.story_query_tools import (
    QueryFeaturesInput,
    query_features_for_stories,
)
from utils.cli_output import emit


def main() -> None:
    """Run a manual query test.

    Note: this script performs database access and must not execute during pytest collection.
    """
    # Test with Project ID 3 (from the logs)
    result = query_features_for_stories(QueryFeaturesInput(project_id=3))

    emit("Query Result Type:", type(result))
    emit("Result is dict:", isinstance(result, dict))

    if result["success"]:
        emit(f"\n✅ Success: {result['message']}")
        emit(f"Project: {result['project_name']} (ID: {result['project_id']})")
        emit(f"Total Features: {result['total_features']}")

        emit(f"\n{'=' * 60}")
        emit("SCHEMA VALIDATION TEST - Features with Theme/Epic:")
        emit(f"{'=' * 60}")

        for i, feat in enumerate(result["features_flat"][:5], 1):  # Show first 5
            emit(f"\n{i}. {feat['feature_title'][:60]}")
            emit(f"   Theme: '{feat['theme']}' (type: {type(feat['theme']).__name__})")
            emit(f"   Epic: '{feat['epic']}' (type: {type(feat['epic']).__name__})")
            emit(f"   Feature ID: {feat['feature_id']}")

            # Verify required fields are NOT None or "Unknown"
            assert feat["theme"], (
                f"FAIL: Theme is empty for feature {feat['feature_id']}"
            )
            assert feat["epic"], f"FAIL: Epic is empty for feature {feat['feature_id']}"
            assert feat["theme"] != "Unknown", (
                f"FAIL: Theme is 'Unknown' for feature {feat['feature_id']}"
            )
            assert feat["epic"] != "Unknown", (
                f"FAIL: Epic is 'Unknown' for feature {feat['feature_id']}"
            )

        emit(f"\n{'=' * 60}")
        emit("✅ ALL SCHEMA VALIDATIONS PASSED!")
        emit(f"{'=' * 60}")
        emit("\nPydantic enforces (internally):")
        emit("  - theme: str (min_length=1, REQUIRED)")
        emit("  - epic: str (min_length=1, REQUIRED)")
        emit("  - No None or empty strings allowed")
    else:
        emit(f"❌ Query failed: {result.get('error', result)}")


if __name__ == "__main__":
    main()
