from services.code_extracter import get_repo
from services.file_finder import export_target_files


def run_pipeline(url: str, codebase_dir: str = "codebase",
                 out_dir: str = "target_files", max_depth: int = 6) -> dict:
    """Clone a repo, find its API files (direct + indirect), copy them to out_dir.

    Returns the manifest: {relative_path: {"type": "direct"|"indirect", "reasons": [...]}}
    """
    print(f"[1/2] Cloning {url} -> {codebase_dir}/")
    get_repo(url)  # clones into "codebase" (its default target)

    print("[2/2] Scanning for API files...")
    manifest = export_target_files(root=codebase_dir, out=out_dir, max_depth=max_depth)

    direct = sum(v["type"] == "direct" for v in manifest.values())
    print(f"Done: {len(manifest)} files in {out_dir}/ "
          f"({direct} direct, {len(manifest) - direct} indirect)")
    return manifest


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        sys.exit("Usage: python -m services.pipeline <github_url>")
    run_pipeline(sys.argv[1])