# Final Independent Review of Phase 0 Contract v1.10

Date: 2026-07-15  
Reviewer: Codex  
Historical baseline: `ed1a1b0d62452f75212da4f78f3e8b9f990da42e`  
Current contract baseline: `d66fce1840c1dcf907c9d303bbc64654c7c41857`  
Reviewed artifact SHA-256:
`05c1bb974193e973025ac818235a9df511c0f566bba4d40f6b270dcaed531128`

## 1. Final verdict

**`approve_for_human_decision`. Phase 0 is closed. Product implementation is still not
automatically authorised.**

The three v1.9 blocking findings are closed at the executable-contract level:

1. the runner construction API no longer accepts a caller-selected installation root;
2. a receipt must carry exactly the policy-authority set resolved by the attestor; and
3. Agent registry, public-contract, and resolved-version claims are compared with a runner-bound
   public-boundary observation adapter and fail closed when no adapter is configured.

Criterion 4 is correctly named `conditional_design_pending_D16`, not `met`. A trusted runner and
receipt writer remain human/operational decisions. Criterion 5 is met as a construction contract,
not as a same-process security boundary.

Two non-blocking test-harness defects remain and are mandatory implementation prerequisites. They
do not justify another Claude/Codex contract revision loop because neither changes the current
allow/deny semantics and both have independent reproductions below.

## 2. Independent evidence

Integrity:

- v1.10 artifact digest matched the handoff value;
- v1.10 published source remained 45/45 after all executions; and
- v1.1 through v1.9 were not modified.

Published execution reproduced:

- contract self-test: 66/66, zero mismatches, coverage PASS;
- vertical paths: Change allow and Output allow positives succeeded; required negatives denied;
- regressions: v1.8 8/8, v1.7 7/7, v1.6 4/4, v1.5 8/8;
- self-attack: 4/14 disclosed bypasses reproduced, all mapped to D-16; and
- v1.9 regression: its first six checks closed, then its new deterministic Git fixture failed to
  initialise on this second run because its fixed output directory already existed.

Independent executable:

- file: `artifacts/codex-v1.10-final-check.py`
- SHA-256: `3a8fa7ee2f54007f6b51a8ca54c54034e73e52134830e9717758990c6cc6b36a`
- result: 5/5 on two consecutive executions.

```text
runner_root_not_an_assessed_call_argument: True
receipt_exact_set_is_generic: True (severity-policy omitted -> deny)
resolved_policy_set_is_current_and_future_fail_closed: True
observer_is_construction_bound_and_default_fail_closed: True
pre_fix_porcelain_fails_while_v110_passes: True
```

The old parser produced `('env.sample', 'z.tmp')`; v1.10 produced
`('.env.sample', 'z.tmp')` and read committed bytes instead of worktree bytes.

## 3. Final judgements

### Criterion 5: approved as a construction contract

`Attestor.for_runner(repo, now, authority)` has no source/policy/installation-root argument.
Passing `src_dir` is rejected and `authority.policy_dir/source` do not affect policy resolution.
The subject repository also cannot become the policy root.

Importing a copied or shadowed `attest` module, changing `sys.path`, modifying `__file__`, or
symlinking the installed policy tree controls which code is installed and executed. Those are
runner/code-provenance attacks under D-16/D-17, not disguised assessment-call inputs. The public
test factory and private sentinel remain importable for the same reason. v1.10 does not claim to
make Python a security sandbox.

### Criterion 4: `conditional_design_pending_D16` is correct

HL-19 now validates receipt identity, decision/evidence, subject, report/manifest bindings, exact
policy-key set, and every policy digest. It still cannot authenticate who wrote a correctly formed
receipt. A protected single writer, signed receipt, or equivalent trusted provenance mechanism must
be selected and verified by a human under D-16. No option is selected by this contract.

### HL-20 observer: legitimate trust boundary, not an assessed self-report

The runtime document can no longer certify its own observation. HL-20 compares it with a separate
adapter bound when the attestor is constructed; `attest_output` accepts no observer argument. The
default shipped adapter raises and fails closed.

A lying adapter can still authorise a lie. That is the same trust shape as a lying event, boundary,
or receipt store and belongs to D-16. The future production adapter must derive evidence from public
Agent symbols/output only. Until D-21 exposes the producer-resolved version and D-22 approves a
version, no production Output may be assured.

### Exact policy set: current rule is structurally fail-closed

HL-19 derives expected policy keys from `resolved_digests`; omission of a different policy
(`severity-policy`) and simulation of a future policy both denied with
`receipt_policy_set_mismatch`. Receipt generation derives its policy map from the same resolved
digest map.

The self-test still manually duplicates the six policy IDs. Its current list matches, but a future
policy addition could let the self-test model drift again without structurally noticing. This is a
test-maintenance defect, not a current receipt-rule bypass.

## 4. Residual test debt required before implementation

### P1. Make the v1.9 Git regression idempotent on Windows

`codex_v19_regression.py` uses the fixed directory `.out/v19-porcelain-subject`, calls
`shutil.rmtree(..., ignore_errors=True)`, and then unconditionally calls `mkdir`. On this Windows
workspace the cleanup failed silently, so the published rerun raised `FileExistsError`.

Use a unique temporary fixture directory under a runner-owned temp root, or fail explicitly when
cleanup fails. Add a test that runs the regression twice consecutively.

### P1. Centralise the policy-authority ID set

`Attestor.resolved_digests()` and `contract_self_test.py::DIGESTS` each contain a separate literal
policy tuple. Replace the self-test duplicate with the same production-owned policy registry or
derive expected IDs from an actual attestor resolution. Add a mutation case that introduces a
future policy and proves the test model changes with it.

These two items belong in the implementation acceptance criteria. They do not require v1.11.

## 5. Human decisions now required

Do not resolve or assign D-1 through D-24 automatically. The minimum blocking decisions before
enforcement implementation are:

- **D-16:** trusted runner, authority adapters, and receipt-writer provenance model;
- **D-17:** protected policy-tree and code-loading model;
- **D-21:** whether the Agent owner exposes the producer-resolved version at the public boundary;
- **D-22:** which production Agent versions, if any, are approved;
- **D-23:** behaviour for an unknown or unapproved default version; and
- **D-24:** session-pinned or per-turn version selection.

The owner names, rollout dates, signing choice, and enforcement mode remain human decisions.

## 6. Next phase

1. Record the human decisions above.
2. Turn the contract into implementation slices, beginning with Change Assurance in Observe mode.
3. Implement Backend Output Boundary Assurance without modifying `app/agent/**`.
4. Wire Evidence and Governance after the two enforcement points produce stable evidence.
5. Move Observe to Warn and Enforce only when the contract's promotion evidence is satisfied.

No more Phase 0 Claude/Codex revision is requested. Future review evaluates the implementation
against this contract rather than rewriting the contract again.

# 한국어 버전

## K1. 최종 판정

**`approve_for_human_decision`입니다. Phase 0 계약 검토는 종료합니다. 다만 제품 구현이 자동으로 승인된
것은 아닙니다.**

v1.9에서 남았던 세 가지 차단 결손은 실행 계약 수준에서 닫혔습니다.

1. runner 생성 API에서 호출자가 설치 루트를 고를 수 없습니다.
2. Change 영수증은 attestor가 해석한 정책 권위 집합과 정확히 같은 키를 가져야 합니다.
3. Agent registry, 공개 계약 버전, 실제 생산 버전 주장은 runner 구성 시 결속된 공개 경계 adapter 관측값과
   비교되며, adapter가 없으면 fail-close합니다.

기준 4는 단순 `충족`이 아니라 `conditional_design_pending_D16`이 맞습니다. 기준 5는 생성 계약으로는
충족했지만 같은 Python 프로세스를 보안 sandbox로 만들었다는 뜻은 아닙니다.

## K2. 검증 결과

- v1.10 문서 SHA 일치
- 실행 원본 45/45 무결성 유지
- self-test 66/66, coverage PASS
- Change와 Output 정상 수직 경로 성공, 필수 부정 경로 거부
- v1.8 8/8, v1.7 7/7, v1.6 4/4, v1.5 8/8
- 공개 자기공격 4/14 재현, 모두 D-16 경계로 분류
- 독립 검증 5/5를 연속 두 번 통과

독립 검증은 `severity-policy`를 제거한 영수증도 거부했고, 미래 정책이 추가된 상황도 fail-close하며,
observer가 검증 호출 인자가 아니고 기본 미구성 상태에서 거부되는 것을 확인했습니다. 이전 Git 파서는
`.env.sample`을 `env.sample`로 잘못 읽었고 v1.10은 정확한 경로와 커밋 객체 내용을 읽었습니다.

## K3. 핵심 판단

### 기준 5

`for_runner`에는 source, policy, installation root 인자가 없습니다. 다른 위치에서 복사한 모듈을 import하거나
`sys.path`, `__file__`, 설치 symlink를 바꾸는 것은 어떤 코드를 실행할지 장악한 공격입니다. 이는 평가 요청
입력 우회가 아니라 D-16/D-17의 runner와 코드 공급망 문제입니다.

### 기준 4

영수증의 ID, 판정, 증거 상태, 대상, 보고서, manifest, 정책 키 집합과 digest는 검증됩니다. 그러나 올바른
형식의 영수증을 실제로 누가 썼는지는 해시만으로 인증할 수 없습니다. 보호된 단일 writer, 서명 또는 동등한
출처 보증 중 하나를 사람이 D-16에서 선택해야 합니다.

### HL-20 observer

평가 문서가 스스로 “관측했다”고 주장해서는 통과하지 못합니다. 별도 runner adapter와 비교하고, 기본
adapter는 미구성이므로 실패합니다. 거짓 adapter까지 신뢰한다는 한계는 event/boundary/receipt store와 같은
D-16 신뢰 경계입니다. D-21과 D-22 전에는 실제 운영 Output을 assured로 만들면 안 됩니다.

## K4. 구현 전에 반드시 갚을 테스트 부채

두 문제는 현재 판정 우회는 아니므로 v1.11을 만들지 않고 구현 인수 기준으로 넘깁니다.

1. `codex_v19_regression.py`의 고정 Git fixture 디렉터리가 Windows에서 정리되지 않아 두 번째 실행이
   `FileExistsError`로 실패합니다. 고유 임시 디렉터리를 사용하고 같은 테스트를 연속 두 번 실행해야 합니다.
2. `Attestor.resolved_digests()`와 self-test가 정책 ID 여섯 개를 각각 수동으로 적고 있습니다. 하나의
   production-owned 정책 registry로 통합하고 미래 정책 추가 mutation을 검사해야 합니다.

## K5. 이제 사람에게 필요한 결정

최소 D-16, D-17, D-21, D-22, D-23, D-24를 사람이 결정해야 합니다. 담당자 이름, 서명 사용 여부, rollout
시점, Observe/Warn/Enforce 전환은 AI가 임의로 정하지 않습니다.

그 후 Change Assurance를 Observe로 먼저 구현하고, Agent 내부를 건드리지 않는 Backend Output Boundary,
Evidence & Governance 순서로 진행합니다. 앞으로는 계약 문서를 다시 반복 작성하지 않고, 실제 구현이 이
계약을 지키는지 검증합니다.
