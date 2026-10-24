"""Deploy ShopAssist to a free Hugging Face Space (CPU Basic: 2 vCPU, 16 GB RAM) in one command.

    hf auth login                     # once; paste a token with "write" access (huggingface.co/settings/tokens)
    python -m scripts.deploy_hf --space <your-username>/shopassist --access-code <code-for-interviewers> --set-secret

What it does:
1. creates the Space (Docker SDK) if it doesn't exist;
2. stores GEMINI_API_KEY (from your local .env, with --set-secret) and DEMO_ACCESS_CODE as encrypted Space
   secrets: never in the code, never visible on the Space page;
3. uploads the code, but never .env, the database or the crawled Amazon pages: the Space crawls the public
   help pages itself while building, so the content is not redistributed through the repository;
4. uses deploy/huggingface/README.md as the Space card (Hugging Face reads the Space config from it).
"""
import argparse
from pathlib import Path

from dotenv import dotenv_values
from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parent.parent
IGNORE = [
    ".env", ".env.*", ".git/*", "*.db", "*.db-*", "**/__pycache__/*", ".pytest_cache/*",
    "knowledge_base/amazon_in/*", "knowledge_base/raw/*", "index/*",
    "evals/reports/*.jsonl", "evals/reports/*.json", "docs/screenshots/*", "README.md",
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--space", required=True, help="e.g. your-username/shopassist")
    ap.add_argument("--private", action="store_true", help="only you and people you invite can open it")
    ap.add_argument("--access-code", help="visitors must enter this code (strongly recommended for a public Space)")
    ap.add_argument("--set-secret", action="store_true", help="copy GEMINI_API_KEY from .env into the Space secrets")
    ap.add_argument("--cors-origin", help="allow a UI hosted elsewhere to call this API, e.g. https://shopassist.vercel.app")
    ap.add_argument("--include-kb", action="store_true",
                    help="upload your locally crawled pages instead of crawling during the build (private Spaces only)")
    args = ap.parse_args()
    ignore = list(IGNORE)
    if args.include_kb:
        # The pages are Amazon's text: only ship them inside a private Space, never a public repository.
        if not args.private:
            raise SystemExit("--include-kb uploads Amazon's help-page text; use it only together with --private.")
        ignore = [p for p in ignore if not p.startswith("knowledge_base/")]

    api = HfApi()
    api.create_repo(args.space, repo_type="space", space_sdk="docker", private=args.private, exist_ok=True)
    if args.set_secret:
        key = dotenv_values(ROOT / ".env").get("GEMINI_API_KEY")
        if not key:
            raise SystemExit("GEMINI_API_KEY not found in .env")
        api.add_space_secret(args.space, "GEMINI_API_KEY", key)
    if args.access_code:
        api.add_space_secret(args.space, "DEMO_ACCESS_CODE", args.access_code)
    if args.cors_origin:
        api.add_space_variable(args.space, "CORS_ORIGINS", args.cors_origin.rstrip("/"))

    # Space card first, then the code: the final build sees both.
    api.upload_file(path_or_fileobj=ROOT / "deploy" / "huggingface" / "README.md", path_in_repo="README.md",
                    repo_id=args.space, repo_type="space", commit_message="Space card")
    api.upload_folder(folder_path=ROOT, repo_id=args.space, repo_type="space", ignore_patterns=ignore,
                      commit_message="Deploy ShopAssist")
    print(f"Uploaded. The first build takes ~5-10 minutes (installs, crawls the help pages, downloads models).\n"
          f"Watch it here: https://huggingface.co/spaces/{args.space}")


if __name__ == "__main__":
    main()
