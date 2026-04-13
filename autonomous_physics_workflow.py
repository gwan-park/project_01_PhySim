import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests


@dataclass
class WorkflowConfig:
    MY_API_KEY_OpenRouter: str = field(default_factory=lambda: os.getenv("MY_API_KEY_OpenRouter", ""))
    model_map: Dict[str, str] = field(
        default_factory=lambda: {
            "clarifier": "openai/gpt-5.4-mini",
            "planner": "anthropic/claude-opus-4.6",
            "coder": "anthropic/claude-sonnet-4.6",
            "debugger": "openai/gpt-5.4",
            "scaler": "openai/gpt-5.4-mini",
            "reviewer": "openai/gpt-5.4",
        }
    )
    max_debug_iter: int = 5
    timeout_seconds: int = 3600
    workspace: Path = Path("workspace")
    result_dir: Path = Path("results")
    log_dir: Path = Path("logs")
    checkpoint_dir: Path = Path("checkpoints")
    schema_dir: Path = Path("schemas")
    verify_mpi_processes: int = 2
    production_mpi_processes: int = 4
    md_fence: str = "`" * 3
    app_name: str = "AutonomousPhysicsWorkflow"
    max_prompt_chars: int = 12000
    max_output_chars: int = 20000


class AutonomousPhysicsWorkflow:
    def __init__(self, config: WorkflowConfig):
        self.cfg = config
        for path in (self.cfg.workspace, self.cfg.result_dir, self.cfg.log_dir, self.cfg.checkpoint_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.sim_spec_schema = self._load_schema("simulation_spec.schema.json")
        self.report_schema = self._load_schema("report.schema.json")

    @staticmethod
    def get_timestamp() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def log_event(self, name: str, content: str) -> Path:
        path = self.cfg.log_dir / f"{name}_{self.get_timestamp()}.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def _load_schema(self, filename: str) -> Dict[str, Any]:
        path = self.cfg.schema_dir / filename
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _checkpoint_path(self, run_id: str) -> Path:
        return self.cfg.checkpoint_dir / f"{run_id}.json"

    def save_checkpoint(self, run_id: str, state: Dict[str, Any]) -> Path:
        path = self._checkpoint_path(run_id)
        payload = {
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "state": state,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_checkpoint(self, checkpoint_file: str) -> Dict[str, Any]:
        path = Path(checkpoint_file)
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("state", {})

    def tail_truncate(self, text: str, limit: int, label: str = "") -> str:
        if len(text) <= limit:
            return text
        prefix = f"\n...[TRUNCATED {label} - kept tail {limit} chars]...\n"
        return prefix + text[-limit:]

    def extract_python_code(self, text: str) -> str:
        pattern = rf"{self.cfg.md_fence}(?:python|py)?\s*(.*?){self.cfg.md_fence}"
        match = re.search(pattern, text, re.S | re.I)
        return match.group(1).strip() if match else text.strip()

    def extract_json_object(self, text: str) -> Optional[Dict[str, Any]]:
        candidate = text.strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None

        snippet = candidate[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            return None

    def validate_required_structure(self, payload: Dict[str, Any], schema: Dict[str, Any], name: str) -> bool:
        if not schema:
            return False
        if schema.get("type") != "object" or not isinstance(payload, dict):
            return False

        required = schema.get("required", [])
        for key in required:
            if key not in payload:
                self.log_event(f"schema_error_{name}", f"Missing required top-level key: {key}")
                return False

        properties = schema.get("properties", {})
        for key, prop in properties.items():
            if key in payload and prop.get("type") == "object" and "required" in prop:
                if not isinstance(payload[key], dict):
                    self.log_event(f"schema_error_{name}", f"Key '{key}' must be object")
                    return False
                for sub in prop.get("required", []):
                    if sub not in payload[key]:
                        self.log_event(f"schema_error_{name}", f"Missing required key: {key}.{sub}")
                        return False
            if key in payload and prop.get("type") == "array":
                if not isinstance(payload[key], list):
                    self.log_event(f"schema_error_{name}", f"Key '{key}' must be array")
                    return False

        return True

    def call_llm(self, role: str, prompt: str, temperature: float = 0.2) -> Optional[str]:
        if role not in self.cfg.model_map:
            raise ValueError(f"Unknown role: {role}")
        if not self.cfg.MY_API_KEY_OpenRouter:
            return None

        prompt = self.tail_truncate(prompt, self.cfg.max_prompt_chars, label=f"prompt:{role}")

        url = "https://openrouter.ai/api/v1/chat/completions"
        payload = {
            "model": self.cfg.model_map[role],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.cfg.MY_API_KEY_OpenRouter}",
            "Content-Type": "application/json",
            "X-Title": self.cfg.app_name,
        }

        retries = 5
        for i in range(retries):
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=90)
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]

                if role in {"coder", "debugger", "scaler"}:
                    return self.extract_python_code(content)
                return content
            except Exception as exc:  # noqa: BLE001
                if i == retries - 1:
                    self.log_event(f"llm_error_{role}", f"Prompt:\n{prompt}\n\nError:\n{exc}")
                    print(f"   [!] Failed to call Agent [{role}] after {retries} retries: {exc}")
                    return None
                time.sleep(2**i)
        return None

    def enforce_sandbox_policy(self, code: str) -> Optional[str]:
        banned_patterns = [
            r"\bos\.system\s*\(",
            r"\bsubprocess\.",
            r"\bshutil\.rmtree\s*\(",
            r"\bos\.remove\s*\(",
            r"\bos\.rmdir\s*\(",
            r"\beval\s*\(",
            r"\bexec\s*\(",
            r"rm\s+-rf\s+/",
        ]
        for pattern in banned_patterns:
            if re.search(pattern, code):
                return f"Blocked by sandbox policy. Detected unsafe pattern: {pattern}"
        return None

    def run_simulation(self, code: str, name_tag: str, mpi_processes: int) -> Dict[str, str]:
        policy_error = self.enforce_sandbox_policy(code)
        if policy_error:
            return {
                "success": "False",
                "stdout": "",
                "stderr": policy_error,
                "path": "",
                "cmd": "",
            }

        file_path = self.cfg.workspace / f"{name_tag}_{self.get_timestamp()}.py"
        file_path.write_text(code, encoding="utf-8")

        use_mpi = any(lib in code.lower() for lib in ["gpaw", "dolfin", "dolfinx", "mpi4py"])
        mpirun_path = shutil.which("mpirun") if use_mpi else None

        with tempfile.TemporaryDirectory(prefix="physim_exec_", dir=self.cfg.workspace) as temp_dir:
            temp_script = Path(temp_dir) / file_path.name
            temp_script.write_text(code, encoding="utf-8")

            if use_mpi and mpirun_path:
                cmd = [mpirun_path, "-np", str(mpi_processes), "python3", str(temp_script)]
            else:
                cmd = ["python3", str(temp_script)]

            safe_env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONUNBUFFERED": "1",
                "OMP_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
            }
            if "MY_API_KEY_OpenRouter" in os.environ:
                safe_env["MY_API_KEY_OpenRouter"] = os.environ["MY_API_KEY_OpenRouter"]

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.cfg.timeout_seconds,
                    check=False,
                    cwd=temp_dir,
                    env=safe_env,
                )
                full_stdout = result.stdout
                full_stderr = result.stderr
                stdout = self.tail_truncate(full_stdout, self.cfg.max_output_chars, label="stdout")
                stderr = self.tail_truncate(full_stderr, self.cfg.max_output_chars, label="stderr")
                output_text = (stdout + "\n" + stderr).lower()

                numerical_fail = any(
                    marker in output_text
                    for marker in ["nan", "diverged", "not converged", "segmentation fault", "traceback", "error"]
                )

                if len(full_stdout) > self.cfg.max_output_chars or len(full_stderr) > self.cfg.max_output_chars:
                    self.log_event(
                        f"full_exec_log_{name_tag}",
                        json.dumps(
                            {
                                "cmd": " ".join(cmd),
                                "stdout": full_stdout,
                                "stderr": full_stderr,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )

                return {
                    "success": str(result.returncode == 0 and not numerical_fail),
                    "stdout": stdout,
                    "stderr": stderr,
                    "path": str(file_path),
                    "cmd": " ".join(cmd),
                }
            except subprocess.TimeoutExpired:
                return {
                    "success": "False",
                    "stdout": "",
                    "stderr": f"Execution timeout ({self.cfg.timeout_seconds}s)",
                    "path": str(file_path),
                    "cmd": " ".join(cmd),
                }

    def _clarify(self, user_input: str) -> Optional[Dict[str, Any]]:
        prompt = f"""
You are a principal computational physicist and electrical engineer.
Return ONLY a JSON object that conforms to this SimulationSpec schema.
Do not add markdown and do not add comments.

Schema:
{json.dumps(self.sim_spec_schema, ensure_ascii=False)}

User request:
{user_input}
"""
        raw = self.call_llm("clarifier", prompt)
        if not raw:
            return None
        parsed = self.extract_json_object(raw)
        if not parsed:
            self.log_event("clarifier_parse_error", raw)
            return None
        if not self.validate_required_structure(parsed, self.sim_spec_schema, "simulation_spec"):
            self.log_event("clarifier_schema_error", json.dumps(parsed, ensure_ascii=False, indent=2))
            return None
        return parsed

    def _plan(self, spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        plan_schema = {
            "type": "object",
            "required": [
                "problem_definition",
                "governing_equations",
                "boundary_initial_conditions",
                "numerical_methods",
                "verification_criteria",
                "production_criteria",
            ],
            "properties": {
                "problem_definition": {"type": "string"},
                "governing_equations": {"type": "array"},
                "boundary_initial_conditions": {"type": "array"},
                "numerical_methods": {"type": "array"},
                "verification_criteria": {"type": "array"},
                "production_criteria": {"type": "array"},
            },
        }
        prompt = f"""
Create a simulation execution plan from this specification.
Return ONLY JSON with exact keys below:
- problem_definition (string)
- governing_equations (array of strings)
- boundary_initial_conditions (array of strings)
- numerical_methods (array of strings)
- verification_criteria (array of strings)
- production_criteria (array of strings)

Specification JSON:
{json.dumps(spec, ensure_ascii=False)}
"""
        raw = self.call_llm("planner", prompt)
        if not raw:
            return None
        parsed = self.extract_json_object(raw)
        if not parsed:
            self.log_event("planner_parse_error", raw)
            return None
        if not self.validate_required_structure(parsed, plan_schema, "plan"):
            self.log_event("planner_schema_error", json.dumps(parsed, ensure_ascii=False, indent=2))
            return None
        return parsed

    def _code(self, task: str, plan: Dict[str, Any], mode: str = "verify") -> Optional[str]:
        resolution_rule = (
            "STRICT RULE: Use coarse mesh, minimal iterations, and low k-points for QUICK VERIFICATION (<60s)."
            if mode == "verify"
            else "STRICT RULE: Use high-fidelity parameters, fine mesh, and converged k-points for PRODUCTION-GRADE results."
        )
        prompt = f"""
Write robust Python simulation code for material/device physics.

Task:
{task}

Plan JSON:
{json.dumps(plan, ensure_ascii=False)}

Mode: {mode}
Rule: {resolution_rule}

Requirements:
- Save all plots/data files under '{self.cfg.result_dir.as_posix()}/'.
- Use plt.savefig() instead of plt.show().
- Use deterministic seeds where relevant.
- Print key outputs and convergence status to stdout.
- Never run shell commands and never use os.system/subprocess.
- Return ONLY Python code within fenced markdown.
"""
        return self.call_llm("coder", prompt)

    def _debug(self, task: str, code: str, error: str) -> Optional[str]:
        error_tail = self.tail_truncate(error, self.cfg.max_prompt_chars, label="debug_error")
        code_tail = self.tail_truncate(code, self.cfg.max_prompt_chars, label="debug_code")
        prompt = f"""
Fix the physical simulation code and return the full corrected code.

Task:
{task}

Current code:
{code_tail}

Error/output (tail-truncated):
{error_tail}

Requirements:
- Keep scientific intent unchanged.
- Never use os.system/subprocess/eval/exec.
- Return ONLY Python code within fenced markdown.
"""
        return self.call_llm("debugger", prompt)

    def _scale(self, verify_code: str) -> Optional[str]:
        code_tail = self.tail_truncate(verify_code, self.cfg.max_prompt_chars, label="scale_code")
        prompt = f"""
Upgrade ONLY numerical fidelity for the production run.
Keep physical logic and plotting unchanged.
Never add shell execution functions.

Code:
{code_tail}
"""
        return self.call_llm("scaler", prompt)

    def _review(self, task: str, plan: Dict[str, Any], stdout: str) -> Optional[Dict[str, Any]]:
        stdout_tail = self.tail_truncate(stdout, self.cfg.max_prompt_chars, label="review_stdout")
        prompt = f"""
As an expert in electrical devices and materials, return ONLY JSON that conforms exactly to this report schema:
{json.dumps(self.report_schema, ensure_ascii=False)}

Task:
{task}

Plan JSON:
{json.dumps(plan, ensure_ascii=False)}

Simulation logs (tail-truncated):
{stdout_tail}
"""
        raw = self.call_llm("reviewer", prompt)
        if not raw:
            return None
        parsed = self.extract_json_object(raw)
        if not parsed:
            self.log_event("review_parse_error", raw)
            return None
        if not self.validate_required_structure(parsed, self.report_schema, "report"):
            self.log_event("review_schema_error", json.dumps(parsed, ensure_ascii=False, indent=2))
            return None
        return parsed

    def render_report_markdown(self, report_json: Dict[str, Any]) -> str:
        lines = ["# Simulation Report", ""]
        lines.append("## 1) Problem Definition & Goal")
        lines.append(report_json.get("problem_definition", ""))
        lines.append("")

        lines.append("## 2) Simulation Concept")
        lines.append(report_json.get("solution_concept", ""))
        lines.append("")

        lines.append("## 3) Physics Principles & Equations")
        for item in report_json.get("physics_and_equations", []):
            concept = item.get("concept", "")
            eq = item.get("equation_latex", "")
            interp = item.get("interpretation", "")
            lines.append(f"- **{concept}**: `${eq}$")
            if interp:
                lines.append(f"  - {interp}")
        lines.append("")

        lines.append("## 4) Boundary Conditions & Environment")
        for bc in report_json.get("boundary_conditions", []):
            region = bc.get("region", "")
            bc_type = bc.get("type", "")
            value = bc.get("value", "")
            role = bc.get("role", "")
            lines.append(f"- {region}: {bc_type} = {value}" + (f" ({role})" if role else ""))
        lines.append("")

        lines.append("## 5) Physical Interpretation of Results")
        result_meaning = report_json.get("result_meaning", {})
        for finding in result_meaning.get("key_findings", []):
            lines.append(f"- {finding}")
        if result_meaning.get("physical_implication"):
            lines.append(f"- Implication: {result_meaning['physical_implication']}")
        if result_meaning.get("limitations"):
            lines.append("- Limitations:")
            for item in result_meaning["limitations"]:
                lines.append(f"  - {item}")
        lines.append("")

        lines.append("## 6) Numerical Performance & Considerations")
        sens = report_json.get("sensitivity_and_performance", {})
        if sens.get("sensitive_parameters"):
            lines.append("- Sensitive parameters:")
            for item in sens["sensitive_parameters"]:
                lines.append(f"  - {item}")
        if sens.get("numerical_stability"):
            lines.append(f"- Numerical stability: {sens['numerical_stability']}")
        if sens.get("runtime_notes"):
            lines.append(f"- Runtime notes: {sens['runtime_notes']}")
        lines.append("")

        lines.append("## 7) Recommendations for Next Steps")
        for rec in report_json.get("next_recommendations", []):
            title = rec.get("title", "")
            reason = rec.get("reason", "")
            out = rec.get("expected_outcome", "")
            lines.append(f"- **{title}**: {reason}" + (f" | Expected: {out}" if out else ""))

        return "\n".join(lines)

    def run_research_workflow(self, user_task: str, auto_confirm: bool = False, resume_from: Optional[str] = None) -> Dict[str, str]:
        state: Dict[str, Any] = {
            "run_id": self.get_timestamp(),
            "phase": "start",
            "user_task": user_task,
        }

        if resume_from:
            loaded = self.load_checkpoint(resume_from)
            if loaded:
                state.update(loaded)
                user_task = state.get("user_task", user_task)
                print(f"[Resume] Loaded checkpoint: {resume_from}")
                print(f"[Resume] Starting from phase: {state.get('phase', 'unknown')}")

        run_id = state["run_id"]

        print("\n" + "=" * 50)
        print("🚀 [Phase 1] Analysis & Planning")
        print("=" * 50)

        spec = state.get("spec")
        if not spec:
            spec = self._clarify(user_task)
            if not spec:
                return {"success": "False", "reason": "clarification failed"}
            state["spec"] = spec
            state["phase"] = "spec_completed"
            self.save_checkpoint(run_id, state)
        print("\n[Step 1/6: Clarified Specification]\n", json.dumps(spec, ensure_ascii=False, indent=2))

        if not auto_confirm and not resume_from:
            user_confirm = input("\nProceed with this specification? [Press Enter or type feedback]: ").strip()
            if user_confirm:
                print("   > Updating specification based on feedback...")
                updated = self._clarify(f"{user_task}\n\nUser feedback:\n{user_confirm}")
                spec = updated or spec
                state["spec"] = spec
                state["phase"] = "spec_updated"
                self.save_checkpoint(run_id, state)
                print("\n[Updated Specification]\n", json.dumps(spec, ensure_ascii=False, indent=2))

        plan = state.get("plan")
        if not plan:
            plan = self._plan(spec)
            if not plan:
                return {"success": "False", "reason": "planning failed"}
            state["plan"] = plan
            state["phase"] = "plan_completed"
            self.save_checkpoint(run_id, state)
        print("\n[Step 2/6: Simulation Plan Generated]\n", json.dumps(plan, ensure_ascii=False, indent=2))

        if not auto_confirm and not resume_from:
            input("\nPlan reviewed. Press Enter to generate code...")

        print("\n" + "=" * 50)
        print("⚙️ [Phase 2] Verification & Self-Healing")
        print("=" * 50)

        verify_code = state.get("verify_code")
        if not verify_code:
            verify_code = self._code(user_task, plan, mode="verify")
            if not verify_code:
                return {"success": "False", "reason": "code generation failed"}
            state["verify_code"] = verify_code
            state["phase"] = "verify_code_generated"
            self.save_checkpoint(run_id, state)

        verified = state.get("verified", False)
        if not verified:
            print("\n[Step 3/6: Running Verification Code (Low Cost)]")
            for i in range(self.cfg.max_debug_iter):
                print(f"   > Execution Attempt {i + 1}...")
                result = self.run_simulation(verify_code, "verify", self.cfg.verify_mpi_processes)

                if result["success"] == "True":
                    print("   ✅ Code verification successful (Physics logic holds).")
                    verified = True
                    state["verified"] = True
                    state["verify_result"] = result
                    state["phase"] = "verify_completed"
                    self.save_checkpoint(run_id, state)
                    break

                print("   ❌ Execution Failed. Self-healing agent activated...")
                self.log_event(f"verify_fail_{i + 1}", json.dumps(result, ensure_ascii=False, indent=2))
                error_bundle = self.tail_truncate(result["stdout"] + "\n" + result["stderr"], self.cfg.max_prompt_chars, "verify_err")
                fixed = self._debug(user_task, verify_code, error_bundle)
                if not fixed:
                    print("   [!] Debugger agent failed to respond.")
                    break
                verify_code = fixed
                state["verify_code"] = verify_code
                state["phase"] = f"verify_retry_{i + 1}"
                self.save_checkpoint(run_id, state)

        if not verified:
            return {"success": "False", "reason": "verification failed", "checkpoint": str(self._checkpoint_path(run_id))}

        print("\n" + "=" * 50)
        print("🔬 [Phase 3] Production Run & Analysis")
        print("=" * 50)

        print("\n[Step 4/6: Scaling up to Production Fidelity]")
        production_code = state.get("production_code")
        if not production_code:
            production_code = self._scale(verify_code) or verify_code
            state["production_code"] = production_code
            state["phase"] = "production_code_generated"
            self.save_checkpoint(run_id, state)

        prod_result = state.get("prod_result")
        if not prod_result:
            print("\n[Step 5/6: Executing Production Run (High Cost)]")
            print(f"   > Utilizing {self.cfg.production_mpi_processes} MPI cores if applicable...")
            prod_result = self.run_simulation(production_code, "production", self.cfg.production_mpi_processes)
            state["prod_result"] = prod_result
            state["phase"] = "production_executed"
            self.save_checkpoint(run_id, state)

        if prod_result["success"] != "True":
            self.log_event("production_fail", json.dumps(prod_result, ensure_ascii=False, indent=2))
            print("   ❌ Production run failed. Check logs.")
            return {"success": "False", "reason": "production failed", "checkpoint": str(self._checkpoint_path(run_id))}

        print("   ✅ Production computation finished successfully.")

        print("\n[Step 6/6: Synthesizing Final Research Report]")
        report_json = state.get("report_json")
        if not report_json:
            report_json = self._review(user_task, plan, prod_result["stdout"])
            if not report_json:
                report_json = {
                    "summary": "Report generation failed",
                    "problem_definition": "N/A",
                    "solution_concept": "N/A",
                    "physics_and_equations": [],
                    "boundary_conditions": [],
                    "result_meaning": {
                        "key_findings": [],
                        "physical_implication": "N/A",
                        "limitations": ["LLM report schema validation failed"],
                    },
                    "sensitivity_and_performance": {
                        "sensitive_parameters": [],
                        "numerical_stability": "Unknown",
                        "runtime_notes": "Report parsing failed",
                    },
                    "next_recommendations": [],
                }
            state["report_json"] = report_json
            state["phase"] = "report_json_completed"
            self.save_checkpoint(run_id, state)

        report_md = self.render_report_markdown(report_json)
        report_path = self.cfg.result_dir / f"Research_Report_{self.get_timestamp()}.md"
        report_path.write_text(report_md, encoding="utf-8")
        state["report_path"] = str(report_path)
        state["phase"] = "completed"
        self.save_checkpoint(run_id, state)

        print("\n" + "=" * 60)
        print(f"📜 Workflow Complete! Report saved to: {report_path}")
        print(f"💾 Checkpoint saved to: {self._checkpoint_path(run_id)}")
        print("=" * 60)
        print(report_md)

        return {
            "success": "True",
            "report": str(report_path),
            "stdout": prod_result["stdout"],
            "cmd": prod_result["cmd"],
            "checkpoint": str(self._checkpoint_path(run_id)),
        }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Autonomous physics simulation workflow (OpenRouter multi-agent)")
    parser.add_argument("--task", type=str, help="Natural-language simulation request")
    parser.add_argument("--auto-confirm", action="store_true", help="Skip interactive confirmation (headless mode)")
    parser.add_argument(
        "--resume-from",
        type=str,
        default=None,
        help="Resume from a checkpoint JSON file (e.g., checkpoints/<run_id>.json)",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    cfg = WorkflowConfig()
    workflow = AutonomousPhysicsWorkflow(cfg)

    if not cfg.MY_API_KEY_OpenRouter:
        print("⚠️ MY_API_KEY_OpenRouter is not set. Set environment variable before running:")
        print("   export MY_API_KEY_OpenRouter='your_api_key_here'")
        return

    print("=== Autonomous Physics Research System (OpenRouter OOP) ===")
    print("Stack target: FEniCS, GPAW, ASE, PyTorch, MPI integration")

    if args.resume_from:
        checkpoint_state = workflow.load_checkpoint(args.resume_from)
        task = checkpoint_state.get("user_task", "")
        if not task and not args.task:
            print("Checkpoint has no task and --task not provided. Exiting.")
            return
    else:
        checkpoint_state = {}

    task = args.task or checkpoint_state.get("user_task") or input("\nEnter your materials/device engineering task:\n> ").strip()
    if not task:
        print("Task is empty. Exiting.")
        return

    result = workflow.run_research_workflow(task, auto_confirm=args.auto_confirm, resume_from=args.resume_from)
    workflow.log_event("final_execution_state", json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
