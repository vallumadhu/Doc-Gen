import ast
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables from the .env file beside this script.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

api_key = os.getenv("OPENROUTER_API_KEY")
if not api_key:
    raise ValueError("OPENROUTER_API_KEY not found in .env")

client = OpenAI(
    api_key=api_key,
    base_url="https://openrouter.ai/api/v1",
)

MODEL = "openai/gpt-4o-mini"
TARGET_DIR = BASE_DIR / "target_files"
MANIFEST_PATH = TARGET_DIR / "manifest.json"
OUTPUT_PATH = BASE_DIR / "generated_api_docs.json"


def read_manifest():
    """Load the manifest produced by the repository scanning pipeline."""
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(
            f"Manifest not found at: {MANIFEST_PATH}\n"
            "Check where your pipeline saves manifest.json."
        )

    with MANIFEST_PATH.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def read_python_files(manifest):
    """Read Python files listed in the manifest, separating direct/supporting files."""
    direct_files = {}
    supporting_files = {}

    for relative_path, info in manifest.items():
        normalized_path = relative_path.replace("\\", "/")
        file_path = TARGET_DIR / normalized_path

        if not file_path.is_file() or file_path.suffix.lower() != ".py":
            continue

        try:
            source = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            print(f"Skipping {relative_path}: {error}")
            continue

        if info.get("type") == "direct":
            direct_files[normalized_path] = source
        else:
            supporting_files[normalized_path] = source

    return direct_files, supporting_files


def _decorator_routes(node):
    """Return method/path pairs for common FastAPI and Flask route decorators."""
    routes = []

    for decorator in node.decorator_list:
        # Handles @router.get("/x"), @app.post("/x"), @app.route("/x")
        if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
            decorator_name = decorator.func.attr.lower()
            route = None

            if decorator.args and isinstance(decorator.args[0], ast.Constant):
                route = decorator.args[0].value

            if not isinstance(route, str):
                continue

            if decorator_name in {
                "get", "post", "put", "patch", "delete", "options", "head", "trace"
            }:
                routes.append({"method": decorator_name.upper(), "path": route})

            elif decorator_name in {"api_route", "route"}:
                methods = None
                for keyword in decorator.keywords:
                    if keyword.arg == "methods" and isinstance(keyword.value, (ast.List, ast.Tuple, ast.Set)):
                        methods = [
                            item.value.upper()
                            for item in keyword.value.elts
                            if isinstance(item, ast.Constant) and isinstance(item.value, str)
                        ]
                if methods is None:
                    methods = ["GET"]
                for method in methods:
                    routes.append({"method": method, "path": route})

    return routes


def _parameter_details(node):
    """Extract function parameters and annotations; API location is inferred later by the model."""
    result = []
    positional = list(node.args.posonlyargs) + list(node.args.args)
    defaults_start = len(positional) - len(node.args.defaults)

    for index, arg in enumerate(positional):
        annotation = ast.unparse(arg.annotation) if arg.annotation else "Not specified"
        result.append({
            "name": arg.arg,
            "type": annotation,
            "required": index >= defaults_start,
        })

    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        annotation = ast.unparse(arg.annotation) if arg.annotation else "Not specified"
        result.append({
            "name": arg.arg,
            "type": annotation,
            "required": default is None,
        })

    if node.args.vararg:
        result.append({
            "name": "*" + node.args.vararg.arg,
            "type": "Not specified",
            "required": False,
        })

    if node.args.kwarg:
        result.append({
            "name": "**" + node.args.kwarg.arg,
            "type": "Not specified",
            "required": False,
        })

    return result


def get_route_details(source, file_path):
    """Parse one source file and return its route-handler functions."""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        print(f"Could not parse {file_path}: {error}")
        return []

    lines = source.splitlines()
    endpoints = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        routes = _decorator_routes(node)
        if not routes:
            continue

        decorator_lines = [decorator.lineno for decorator in node.decorator_list]
        start_line = min([node.lineno] + decorator_lines)
        end_line = node.end_lineno or node.lineno
        original_code = "\n".join(lines[start_line - 1:end_line])

        for route_info in routes:
            endpoints.append({
                "method": route_info["method"],
                "path": route_info["path"],
                "function_name": node.name,
                "parameters": _parameter_details(node),
                "original_code": original_code,
                "file_path": file_path,
            })

    return endpoints


def find_supporting_code(endpoint, supporting_files):
    """Find helper functions called directly by the endpoint."""
    called_functions = set()

    try:
        tree = ast.parse(endpoint["original_code"])
    except SyntaxError:
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called_functions.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called_functions.add(node.func.attr)

    context = []
    for path, source in supporting_files.items():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in called_functions
            ):
                function_source = ast.get_source_segment(source, node)
                if function_source:
                    context.append(f"File: {path}\n{function_source}")

    return "\n\n".join(context)


def generate_documentation(endpoint, supporting_files):
    """Ask OpenRouter for a single endpoint in the required JSON schema."""
    context = find_supporting_code(endpoint, supporting_files)

    prompt = f"""
Generate API documentation for this endpoint. Return ONLY one valid JSON object.
Do not use Markdown fences.

Required JSON shape:
{{
  "method": "POST",
  "path": "/example",
  "summary": "Short summary",
  "description": "Description supported by source code",
  "parameters": [
    {{
      "name": "parameter name",
      "type": "parameter type",
      "required": true,
      "in": "path, query, body, file, or unknown"
    }}
  ],
  "responses": {{
    "200": {{
      "description": "Response description"
    }}
  }}
}}

Rules:
- Use only behavior supported by the provided source.
- Do not invent database behavior, parameters, or response status codes.
- Document explicit error responses when present.
- If a detail is unknown, say "Not specified in the code".
- Include request-body model fields only if their model definitions are provided.
- The original function parameters are supplied as hints; determine their locations
  from FastAPI declarations such as Query, Path, Body, File, or UploadFile where possible.
- Return valid JSON only.

HTTP method: {endpoint["method"]}
Route: {endpoint["path"]}
Function: {endpoint["function_name"]}
Source file: {endpoint["file_path"]}
Parsed parameters: {json.dumps(endpoint["parameters"], ensure_ascii=False)}

Original endpoint code:
{endpoint["original_code"]}

Relevant supporting code:
{context or "No matching supporting function found."}
"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=1200,
    )

    content = response.choices[0].message.content
    if not content:
        raise ValueError("The model returned an empty response.")

    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        if content.endswith("```"):
            content = content[:-3].strip()

    documentation = json.loads(content)

    required_fields = {
        "method", "path", "summary", "description", "parameters", "responses"
    }
    if not required_fields.issubset(documentation):
        raise ValueError("Generated documentation is missing required fields.")

    # Preserve route/method values extracted from source rather than model guesses.
    documentation["method"] = endpoint["method"]
    documentation["path"] = endpoint["path"]
    return documentation


def main():
    print("Reading manifest...")
    manifest = read_manifest()

    print("Reading extracted Python files...")
    direct_files, supporting_files = read_python_files(manifest)
    print(f"Direct Python files: {len(direct_files)}")
    print(f"Supporting Python files: {len(supporting_files)}")

    endpoints = []
    for file_path, source in direct_files.items():
        print(f"Scanning: {file_path}")
        endpoints.extend(get_route_details(source, file_path))

    print(f"\nDetected {len(endpoints)} endpoint(s).")
    if not endpoints:
        print("No supported API routes were detected.")
        return

    results = []
    for index, endpoint in enumerate(endpoints, start=1):
        print(f"\n[{index}/{len(endpoints)}] {endpoint['method']} {endpoint['path']}")
        try:
            documentation = generate_documentation(endpoint, supporting_files)
            results.append(documentation)
            print("Documentation generated successfully.")
        except Exception as error:
            print(f"Failed to document endpoint: {error}")

    output = {
        "title": "Generated API Documentation",
        "description": "Documentation generated from the analyzed repository.",
        "endpoints": results,
    }

    OUTPUT_PATH.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nFinished! Documented {len(results)} endpoint(s).")
    print(f"Output saved to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
