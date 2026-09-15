from pathlib import Path

WORKFLOW = Path(__file__).parents[2] / ".github/workflows/publish-dashboard-ui.yml"


def test_dashboard_release_upload_is_compatible_with_immutable_releases() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "--json isImmutable" in workflow
    assert 'dashboard_tag="dashboard-${REF_NAME}"' in workflow
    assert 'gh release create "${dashboard_tag}"' in workflow
    assert '"${dashboard_asset}"' in workflow
    assert "--json isDraft" in workflow
    assert 'gh release edit "${dashboard_tag}" --draft=false --latest=false' in workflow


def test_new_package_release_attaches_asset_during_creation() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    create_primary = workflow.index('gh release create "${REF_NAME}"')
    immutable_check = workflow.index("immutable=$(gh release view")
    primary_block = workflow[create_primary:immutable_check]

    assert '"${asset}"' in primary_block
    assert "exit 0" in primary_block
