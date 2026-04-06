import argparse
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import requests


@dataclass
class WorkflowConfig:
    openrouter_api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    model_map: Dict[str, str] = field(
        default_factory=lambda: {
            "clarifier": "anthropic/claude-3-haiku",
            "planner": "openai/gpt-4o",
            "coder": "deepseek/deepseek-coder",
            "debugger": "anthropic/claude-3.5-sonnet",
            "scaler": "openai/gpt-4o",
            "reviewer": "openai/gpt-4o",
        }
    )
    max_debug_iter: int = 5
    timeout_seconds: int = 3600
    workspace: Path = Path("workspace")
    result_dir: Path = Path("results")
    log_dir: Path = Path("logs")
    verify_mpi_processes: int = 2
    production_mpi_processes: int = 4
    md_fence: str = "`" * 3
    app_name: str = "AutonomousPhysicsWorkflow"


class AutonomousPhysicsWorkflow:
    def __init__(self, config: WorkflowConfig):
        self.cfg = config
        for path in (self.cfg.workspace, self.cfg.result_dir, self.cfg.log_dir):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def get_timestamp() -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def log_event(self, name: str, content: str) -> Path:
        path = self.cfg.log_dir / f"{name}_{self.get_timestamp()}.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def extract_python_code(self, text: str) -> str:
        pattern = rf"{self.cfg.md_fence}(?:python|py)?\s*(.*?){self.cfg.md_fence}"
        match = re.search(pattern, text, re.S | re.I)
        return match.group(1).strip() if match else text.strip()

    def call_llm(self, role: str, prompt: str, temperature: float = 0.2) -> Optional[str]:
        if role not in self.cfg.model_map:
            raise ValueError(f"Unknown role: {role}")
        if not self.cfg.openrouter_api_key:
            return None

        url = "https://openrouter.ai/api/v1/chat/completions"
        payload = {
            "model": self.cfg.model_map[role],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.cfg.openrouter_api_key}",
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

    def run_simulation(self, code: str, name_tag: str, mpi_processes: int) -> Dict[str, str]:
        file_path = self.cfg.workspace / f"{name_tag}_{self.get_timestamp()}.py"
        file_path.write_text(code, encoding="utf-8")

        use_mpi = any(lib in code.lower() for lib in ["gpaw", "dolfin", "dolfinx", "mpi4py"])
        mpirun_path = shutil.which("mpirun") if use_mpi else None

        if use_mpi and mpirun_path:
            cmd = [mpirun_path, "-np", str(mpi_processes), "python3", str(file_path)]
        else:
            cmd = ["python3", str(file_path)]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.cfg.timeout_seconds, check=False)
            output_text = (result.stdout + "\n" + result.stderr).lower()
            numerical_fail = any(
                marker in output_text
                for marker in ["nan", "diverged", "not converged", "segmentation fault", "traceback", "error"]
            )

            return {
                "success": str(result.returncode == 0 and not numerical_fail),
                "stdout": result.stdout,
                "stderr": result.stderr,
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

    def _clarify(self, user_input: str) -> Optional[str]:
        prompt = f"""
You are a principal computational physicist and electrical engineer.
Clarify the user request and produce a precise simulation specification.

Must include:
1) Physical objective and measurable outputs.
2) Scale classification (atomistic/device/continuum/multiscale).
3) Recommended stack among ASE+GPAW, FEniCS/dolfinx, PyTorch.
4) Required inputs (materials, geometry, boundary conditions, runtime budget).
5) Any unresolved assumptions as a short checklist.

User request:
{user_input}
"""
        return self.call_llm("clarifier", prompt)

    def _plan(self, spec: str) -> Optional[str]:
        prompt = f"""
Create a simulation execution plan from this specification.

Format:
- Problem Definition
- Governing Equations (Maxwell, Schrodinger, Poisson, etc.)
- Boundary/Initial Conditions
- Numerical Methods
- Verification Criteria
- Production Criteria

Specification:
{spec}
"""
        return self.call_llm("planner", prompt)

    def _code(self, task: str, plan: str, mode: str = "verify") -> Optional[str]:
        resolution_rule = (
            "STRICT RULE: Use coarse mesh, minimal iterations, and low k-points for QUICK VERIFICATION (<60s)."
            if mode == "verify"
            else "STRICT RULE: Use high-fidelity parameters, fine mesh, and converged k-points for PRODUCTION-GRADE results."
        )
        prompt = f"""
Write robust Python simulation code for material/device physics.

Task:
{task}

Plan:
{plan}

Mode: {mode}
Rule: {resolution_rule}

Requirements:
- Save all plots/data files under '{self.cfg.result_dir.as_posix()}/'.
- Use plt.savefig() instead of plt.show().
- Use deterministic seeds where relevant.
- Print key outputs and convergence status to stdout.
- Return ONLY Python code within fenced markdown.
"""
        return self.call_llm("coder", prompt)

    def _debug(self, task: str, code: str, error: str) -> Optional[str]:
        prompt = f"""
Fix the physical simulation code and return the full corrected code.

Task:
{task}

Current code:
{code}

Error/output:
{error}
"""
        return self.call_llm("debugger", prompt)

    def _scale(self, verify_code: str) -> Optional[str]:
        prompt = f"""
Upgrade ONLY numerical fidelity for the production run.
Keep physical logic and plotting unchanged.

Code:
{verify_code}
"""
        return self.call_llm("scaler", prompt)

    def _review(self, task: str, plan: str, stdout: str) -> Optional[str]:
        prompt = f"""
As an expert in electrical devices and materials, write an English markdown report with EXACT sections:
1) Problem Definition & Goal
2) Simulation Concept
3) Physics Principles & Equations (Use LaTeX for formulas)
4) Boundary Conditions & Environment
5) Physical Interpretation of Results
6) Numerical Performance & Considerations
7) Recommendations for Next Steps

Task:
{task}

Plan:
{plan}

Simulation logs:
{stdout}
"""
        return self.call_llm("reviewer", prompt)

    def run_research_workflow(self, user_task: str, auto_confirm: bool = False) -> Dict[str, str]:
        print("\n" + "=" * 50)
        print("🚀 [Phase 1] Analysis & Planning")
        print("=" * 50)

        spec = self._clarify(user_task)
        if not spec:
            return {"success": "False", "reason": "clarification failed"}
        print("\n[Step 1/6: Clarified Specification]\n", spec)

        if not auto_confirm:
            user_confirm = input("\nProceed with this specification? [Press Enter or type feedback]: ").strip()
            if user_confirm:
                print("   > Updating specification based on feedback...")
                updated = self.call_llm(
                    "clarifier",
                    f"Update the spec using this feedback: {user_confirm}\nOriginal spec:\n{spec}",
                )
                spec = updated or spec
                print("\n[Updated Specification]\n", spec)

        plan = self._plan(spec)
        if not plan:
            return {"success": "False", "reason": "planning failed"}
        print("\n[Step 2/6: Simulation Plan Generated]\n", plan)

        if not auto_confirm:
            input("\nPlan reviewed. Press Enter to generate code...")

        print("\n" + "=" * 50)
        print("⚙️ [Phase 2] Verification & Self-Healing")
        print("=" * 50)

        verify_code = self._code(user_task, plan, mode="verify")
        if not verify_code:
            return {"success": "False", "reason": "code generation failed"}

        verified = False
        print("\n[Step 3/6: Running Verification Code (Low Cost)]")
        for i in range(self.cfg.max_debug_iter):
            print(f"   > Execution Attempt {i + 1}...")
            result = self.run_simulation(verify_code, "verify", self.cfg.verify_mpi_processes)

            if result["success"] == "True":
                print("   ✅ Code verification successful (Physics logic holds).")
                verified = True
                break

            print("   ❌ Execution Failed. Self-healing agent activated...")
            self.log_event(f"verify_fail_{i + 1}", json.dumps(result, ensure_ascii=False, indent=2))
            fixed = self._debug(user_task, verify_code, result["stdout"] + "\n" + result["stderr"])
            if not fixed:
                print("   [!] Debugger agent failed to respond.")
                break
            verify_code = fixed

        if not verified:
            return {"success": "False", "reason": "verification failed"}

        print("\n" + "=" * 50)
        print("🔬 [Phase 3] Production Run & Analysis")
        print("=" * 50)

        print("\n[Step 4/6: Scaling up to Production Fidelity]")
        production_code = self._scale(verify_code) or verify_code

        print("\n[Step 5/6: Executing Production Run (High Cost)]")
        print(f"   > Utilizing {self.cfg.production_mpi_processes} MPI cores if applicable...")
        prod_result = self.run_simulation(production_code, "production", self.cfg.production_mpi_processes)

        if prod_result["success"] != "True":
            self.log_event("production_fail", json.dumps(prod_result, ensure_ascii=False, indent=2))
            print("   ❌ Production run failed. Check logs.")
            return {"success": "False", "reason": "production failed"}

        print("   ✅ Production computation finished successfully.")

        print("\n[Step 6/6: Synthesizing Final Research Report]")
        report = self._review(user_task, plan, prod_result["stdout"]) or "# Report generation failed"
        report_path = self.cfg.result_dir / f"Research_Report_{self.get_timestamp()}.md"
        report_path.write_text(report, encoding="utf-8")

        print("\n" + "=" * 60)
        print(f"📜 Workflow Complete! Report saved to: {report_path}")
        print("=" * 60)
        print(report)

        return {
            "success": "True",
            "report": str(report_path),
            "stdout": prod_result["stdout"],
            "cmd": prod_result["cmd"],
        }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Autonomous physics simulation workflow (OpenRouter multi-agent)")
    parser.add_argument("--task", type=str, help="Natural-language simulation request")
    parser.add_argument("--auto-confirm", action="store_true", help="Skip interactive confirmation (headless mode)")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    cfg = WorkflowConfig()
    workflow = AutonomousPhysicsWorkflow(cfg)

    if not cfg.openrouter_api_key:
        print("⚠️ OPENROUTER_API_KEY is not set. Set environment variable before running:")
        print("   export OPENROUTER_API_KEY='your_api_key_here'")
        return

    print("=== Autonomous Physics Research System (OpenRouter OOP) ===")
    print("Stack target: FEniCS, GPAW, ASE, PyTorch, MPI integration")

    task = args.task or input("\nEnter your materials/device engineering task:\n> ").strip()
    if not task:
        print("Task is empty. Exiting.")
        return

    result = workflow.run_research_workflow(task, auto_confirm=args.auto_confirm)
    workflow.log_event("final_execution_state", json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
