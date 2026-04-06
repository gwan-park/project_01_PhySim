# project_01_PhySim

자연어 기반 물리 시뮬레이션 멀티에이전트 워크플로우 템플릿 + 실행 스크립트 저장소입니다.

## 구성
- 아키텍처/코딩 플랜: `docs/architecture/phy_sim_multi_agent_plan.md`
- 입력 스키마: `schemas/simulation_spec.schema.json`
- 리포트 스키마: `schemas/report.schema.json`
- 에이전트별 프롬프트: `prompts/starter_prompts.md`
- OpenRouter 실행기: `autonomous_physics_workflow.py`
- Obsidian 요약본: `docs/obsidian/phy_sim_workflow_summary_for_obsidian.md`

## 빠른 실행
```bash
export OPENROUTER_API_KEY='your_api_key_here'
python3 autonomous_physics_workflow.py --task "Simulate ion-crystal interaction energy profile" --auto-confirm
```

## 참고
- 스크립트는 OpenRouter를 통해 역할별 모델(clarifier/planner/coder/debugger/scaler/reviewer)을 호출합니다.
- 검증(저비용) → 디버그 반복 → 생산(고충실도) → 보고서 생성 순으로 동작합니다.


## 체크포인트/재개
```bash
python3 autonomous_physics_workflow.py --resume-from checkpoints/<run_id>.json --auto-confirm
```
- `spec`, `plan`, `verify_code`, `production_code`, `report_json`를 단계별 체크포인트로 저장하고 재개할 수 있습니다.
