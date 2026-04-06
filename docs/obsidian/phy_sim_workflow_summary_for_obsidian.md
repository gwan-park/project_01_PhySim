# PhySim 워크플로우 요약 (Obsidian 저장용)

> 목적: 이 문서 하나만 보고도 사람/LLM이 프로젝트를 재현하고 실행할 수 있도록, 현재 코드와 README 핵심을 압축 정리.

## 1) 프로젝트 한 줄 요약
- 자연어로 시뮬레이션 목표를 입력하면, OpenRouter 기반 멀티 에이전트가 **명세 정리 → 계획 수립 → 코드 생성 → 실행/자기복구 → 고충실도 실행 → 결과 리포트 생성**까지 자동으로 수행한다.

## 2) 현재 저장소 구성
- `autonomous_physics_workflow.py`: 메인 실행 엔진 (CLI + 오케스트레이션 + OpenRouter 호출 + 실행/디버그 루프)
- `README.md`: 빠른 실행 가이드
- `docs/architecture/phy_sim_multi_agent_plan.md`: 전체 아키텍처/전략 문서
- `prompts/starter_prompts.md`: 에이전트 프롬프트 템플릿
- `schemas/simulation_spec.schema.json`: 입력 명세 스키마
- `schemas/report.schema.json`: 출력 리포트 스키마

## 3) 코드 플로우 (실행 순서)
`run_research_workflow()` 기준:

1. **Clarify** (`_clarify`)
   - 사용자 자연어 입력을 받아 시뮬레이션 명세로 정리
   - 물리 목표, 스케일, 추천 스택, 필요 입력, 가정 체크리스트 생성
2. **Plan** (`_plan`)
   - 지배방정식/BC/수치기법/검증기준/생산기준 포함한 실행 계획 생성
3. **Code (verify mode)** (`_code`)
   - 저비용 검증용 파라미터(거친 mesh, 낮은 k-point 등)로 코드 생성
4. **Execute + Self-heal loop** (`run_simulation` + `_debug`)
   - 코드 실행 후 실패하면 로그 기반으로 디버거 에이전트가 전체 코드를 수정
   - `max_debug_iter` 횟수만큼 자동 반복
5. **Scale to production** (`_scale`)
   - 물리 로직은 유지하고 수치 충실도만 상향
6. **Production run** (`run_simulation`)
   - 고비용/고충실도 실행
7. **Review/Report** (`_review`)
   - 7개 고정 섹션(문제정의~다음 추천)으로 Markdown 리포트 작성 후 `results/` 저장

## 4) 동작 원리
### A. 역할 기반 모델 라우팅
`WorkflowConfig.model_map`에서 역할별 모델을 지정:
- clarifier / planner / coder / debugger / scaler / reviewer

### B. OpenRouter API 호출
- 엔드포인트: `https://openrouter.ai/api/v1/chat/completions`
- 인증: `OPENROUTER_API_KEY`
- 실패 시 지수 백오프 재시도(최대 5회), 최종 실패 로그 저장

### C. 코드 추출/실행
- LLM 응답에서 fenced code block 추출 (`extract_python_code`)
- `workspace/`에 실행 파일 저장 후 subprocess 실행
- 코드에 `gpaw`, `dolfin(x)`, `mpi4py` 문자열이 있으면 MPI 실행 시도

### D. 실패 감지 규칙
stdout/stderr에 아래 키워드가 있으면 수치 실패로 판단:
- `nan`, `diverged`, `not converged`, `segmentation fault`, `traceback`, `error`

### E. 산출물/로그
- 실행 코드: `workspace/`
- 최종 리포트: `results/`
- 에러/중간 이벤트 로그: `logs/`

## 5) 환경 및 설치
## 필수
- Python 3.10+ (권장 3.11)
- OpenRouter API Key
- 패키지: `requests`

## 선택(시뮬레이션 코드가 해당 라이브러리를 생성할 때 필요)
- `numpy`, `scipy`, `matplotlib`
- `ase`, `gpaw`
- `fenics` 또는 `dolfinx`
- `torch`
- MPI 실행 환경: `mpirun` (OpenMPI/MPICH)

## 빠른 설치 예시
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install requests
```

고급 물리 라이브러리는 OS/컴파일러 의존성이 커서 별도 환경(Conda/HPC 모듈) 설치 권장.

## 6) 실행 방법
## 환경변수 설정
```bash
export OPENROUTER_API_KEY='your_api_key_here'
```

## 헤드리스 실행(자동 확인)
```bash
python3 autonomous_physics_workflow.py \
  --task "Simulate ion-crystal interaction energy profile" \
  --auto-confirm
```

## 인터랙티브 실행
```bash
python3 autonomous_physics_workflow.py
```

## 7) 운영 팁 (사람/LLM 공통)
1. 먼저 저비용 검증 모드가 통과하는지 확인한다.
2. 실패 시 `logs/verify_fail_*.txt`와 `logs/llm_error_*.txt`를 우선 본다.
3. 수치 안정성 이슈는 BC/mesh/timestep/수렴 조건 순서로 재검토한다.
4. 모델 비용 절감을 위해 역할별 모델을 분리하고, coder/debugger 토큰을 제한한다.
5. 재현성을 위해 실험마다 task 문장, model_map, timeout, MPI 코어 수를 함께 기록한다.

## 8) 알려진 한계
- 현재는 단일 파이썬 스크립트 중심 구조로, 대규모 작업 큐/분산 오케스트레이션은 내장돼 있지 않다.
- 물리 타당성 검증은 규칙 기반 키워드 탐지 위주이며, 도메인별 정량 검증(예: 에너지 보존 오차 한계)은 추가 구현이 필요하다.
- 외부 LLM/도구 상태(OpenRouter, MPI, 개별 solver 설치)에 따라 성공률이 달라질 수 있다.

## 9) 다음 확장 추천
- `SimulationSpec`/`Report` JSON 스키마를 실제 런타임 입출력 검증에 강제 연결
- FastAPI + 큐(Celery/RQ)로 비동기 job orchestration
- 실패 분류기(파싱기) 강화: solver별 에러 taxonomy 구축
- 실험 메타데이터(DB) 저장으로 검색/재현 자동화

---
이 문서는 Obsidian에서 바로 보관/링크할 수 있는 단일 요약본이다.
