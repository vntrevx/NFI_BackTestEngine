# Future NFI Compatibility

이 문서는 빠르게 바뀌는 NFI 전략을 정확성과 사용성을 잃지 않고 처리하는 운영
계약을 설명한다. 특정 NFI 버전, Signal 번호, 전략 SHA 또는 결과값은 런타임
동작의 분기 조건으로 사용하지 않는다.

## 사용자 실행 경로

`nfi-bte run`은 먼저 Native 호환성을 검사한다.

```text
latest NFI
├─ exact lowering 가능 → Rust Native 실행
└─ 미지원 의미론 발견 → 공식 Freqtrade fallback 또는 안전한 중단
```

`--fallback official`은 Native가
`blocked_unsupported_semantics`로 끝난 경우에만 pinned Freqtrade를 실행한다.
기본값 `ask`는 대화형 터미널에서만 동의를 묻고, 비대화형 환경에서는 자동으로
공식 실행하지 않는다. `--yes`도 fallback 동의를 뜻하지 않는다.

전환은 조용히 일어나지 않는다. CLI는 Native blocker code와 설명을 먼저 출력하고,
공식 실행 동의 여부, 예상 시간 차이, Native evidence가 변경되지 않는다는 점과
공식-only 결과가 parity 주장이 아니라는 점을 실행 전에 알린다.

공식 결과는 원래 Native run을 수정하지 않고 별도 시도 디렉터리와
`selected-result.json`에 기록한다. 이 결과의 역할은 `official_only`이며
`exact_parity`를 주장하지 않는다. Native 완료, 공식 완료, 사용자가 선택한 결과는
registry, ledger, terminal, `report.md`에서 독립 상태로 표시된다.

## 변경 분석과 Native 승격

다음 명령은 소스 실행 없이 변경과 범용 IR을 검사한다.

```bash
nfi-bte strategy diff old.py new.py -o strategy-diff.json
nfi-bte strategy state-machine new.py -o state-machine-ir.json
nfi-bte strategy qualify compatibility.json strategy-diff.json \
  --branch-proof shadow-proof.json -o qualification.json
nfi-bte strategy verify-targeted new.py strategy-diff.json compatibility.json \
  --class NostalgiaForInfinityX7 --trading-mode spot \
  --upstream-repository iterativv/NostalgiaForInfinity \
  --upstream-commit UPSTREAM_COMMIT --output-dir targeted-spot
```

변경 분석은 Signal/tag, callback, dataframe column, custom trade state, Grind level과
범용 opcode뿐 아니라 변경 단위별 `behavior_targets`를 추출한다. 표적 검증은
fixture 이름이나 Signal 번호를 조건문에 넣지 않고, 이 target과 기존 공식
coverage의 교집합으로 최소 fixture 집합을 결정론적으로 선택한다. 기존 명령
조합으로 표현되는 변경은 전략 소스에서 컴파일한다. 새로운 callback, 동적 키,
무제한 반복, 모호한 stake 방향 또는 관찰할 수 없는 상태 전이는 정확한 소스
위치와 `TARGETED_COVERAGE_GAP`을 남기고 fail-closed한다.

소스 diff는 클래스 mapping의 boolean 변경도 값과 소스 span으로 기록한다. 이전
branch가 꺼져 있었다면 discovery가 이 기록으로 이전 소스의 같은 key만 임시
전환하여 old/new 동작을 비교한다. exit reason은 전체 문자열과 괄호 앞 canonical
route를 함께 관찰하므로 새로 장식된 tag도 기존 route 의미론과 연결되지만, 원문
tag는 증거에 그대로 보존된다.

상태 머신 VM은 source order, step limit, typed custom state, wallet/trade/order read,
추가 진입, 부분 청산과 exit를 실행한다. 실패한 실행은 custom state를 원자적으로
rollback한다. Signal 번호와 Grind 단계 수는 opcode가 아니라 IR 데이터다.

지원되는 X7 stateful 실행은 공개 실행 계약에서 `x7-generic-stateful` lane으로
기록된다. `native_execution.programs`는 전략 소스에서 직렬화된 stateful root와
각 `execution_mode`를 경로별로 보존한다. 현재 contract의 모든 root가 `primary`여야
Native가 시작되며, 빠진 mode나 폐기된 shadow mode는 시뮬레이션 전에 차단된다. X7
전용 vector manifest는 운송 계약일 뿐 기본 동작의 선택 기준이 아니다. 이전 schema
reader는 과거 evidence replay를 위해 유지하지만 현재 실행에는 참여하지 않는다.
fallback은 계속 사용자 동의 또는
`--fallback official`로만 시작하며 실행 전에 반드시 전환 사실을 알린다.

`state-machine-program-v2`는 v1 프로그램도 계속 실행하면서, 동기식
`self.helper(...)`가 순수한 단일 return 계산이면 호출 그래프를 소스에서
전이적으로 인라인한다. helper가 읽는 class mapping/sequence의 threshold와 route
tag도 IR literal이므로 helper 본문이나 데이터가 바뀌면 프로그램 identity가
바뀐다. 재귀, `*args`, 상태를 쓰는 helper, 동적 key 또는 임의 Python 호출은
소스 위치와 함께 차단한다. 전략 버전이나 method SHA를 새 실행 의미론의 선택
조건으로 사용하지 않는다.

`state-machine-program-v3`는 `trade.select_filled_orders(trade.entry_side)`의
소스 순서 반복, typed order field, local 누적과 원자적 custom state 변경을
지원한다. 실행 상한은 Signal·tag가 아니라 전략의 유한한
`max_entry_position_adjustment + 1`에서 계산한다. 공식 범용 fixture는 진입 주문
13개 중 source tag가 일치하는 12개를 세어 `finite_order_exit` 분기에 도달하며,
독립 Native 실행과 trade surface 및 286개 every-candle state가 exact해야 한다.

`quick_verified` 승격에는 최신 전략으로 다시 만든 임시 workload, 서로 독립된
이전 공식·최신 공식·최신 Native 실행, presence/absence/transition branch 증명,
trade surface exact, full-state exact가 모두 필요하다. 원본 fixture는 수정하지
않고 같은 artifact를 양쪽 증거로 재사용할 수 없다.

## 자동 분류 계약

정적 검사, targeted qualification, 선택적인 discovery 결과는 mode별 결정문 하나로
합쳐진다. 결정은 소스에서 추출한 opcode와 `behavior_targets`만 사용하며 전략 버전,
Signal/Grind 번호, pair, timerange 또는 기대 결과를 분기값으로 사용하지 않는다.

- `native_exact`: 변경 branch 도달과 trade surface/full-state exact가 모두 참일 때만 허용
- `semantic_review_issue`: 새 opcode 또는 generic lowering 검토가 필요하며 실행은 official-only
- `bounded_discovery`: 정적 lowering은 가능하지만 exact branch fixture가 부족함
- `exact_fixture_draft_pr`: discovery가 독립 exact 후보를 만들었으나 병합 전 검토가 필요함
- `external_data_deferred`: 외부 데이터 재시도 보류이며 exact 증거가 아님
- `official_only`: 탐색이 끝났거나 현재 Native exact를 증명할 수 없음

semantic review는 append-only ledger와 자동 조정되는 단일 compatibility issue에
기록한다. evidence-only Draft PR은 만들지 않는다. maintainer가 범용 opcode/lowerer,
단위 테스트, 공식 fixture를 추가하고 Required CI와 exact 검증을 통과해야 Native로
승격된다. PR은 독립 exact fixture 후보 또는 실제 구현 변경에만 사용한다. 외부 데이터
보류를 재사용할 때도 저장된 결정문의 `execution_route=official_only`, `exact=false`를
다시 검사한다.

## Managed exit의 단계적 Native 전환

8개 managed-long route의 `custom_exit` 분기 순서와 재귀 any/all matcher, 수익
basis와 gate, mode name, pure decision 호출 순서는 이제
`managed-exit-program-v1` 데이터로 컴파일된다. 복합 rebuy/rapid/scalp tag도
상수값별 Rust 분기 없이 같은 matcher evaluator가 처리한다. quick/rapid inline
조건과 reason도 Scalar IR이며 stop 선택·threshold, target-cache 갱신 간격, 최대
target floor, 보호 신호와 rebuy terminal을 source state program으로 컴파일한다.
Rust는 현재 contract에서 generic 경로만 실행한다. 과거 shadow 비교로 결정과
target-cache 전체 상태의 exact를 독립 증명한 뒤 현재 실행에서 legacy 호출을
제거했으며, 이전 schema reader는 sealed evidence 재생에만 남아 있다.

managed-short도 별도 source compiler로 같은 실행 경계에 들어왔다. short quick/rapid
조건은 long 조건의 부호 반전이 아니라 short AST에서 직접 Scalar IR로 생성된다.
scalp compound route와 pure-scalp target matcher, top-coins normal fallback의
`is_short AND NOT known_tags`도 서로 다른 source predicate로 보존한다. 두 방향 모두
generic 결과가 현재 실행의 유일한 경로다. 구조적으로 컴파일되는 route wrapper,
`custom_exit`, 공통 stop/target 정책의 runtime method-hash gate는 제거했다. 과거
shadow mode는 새 실행에 승계되지 않으며 backward schema reader에서만 허용된다.

Rebuy의 long/short 역순 주문 cluster, ladder 조건과 stake 산식, level-3 de-risk,
결과 tag도 `adjustment-transition-program-v1`으로 소스에서 컴파일한다. dataframe
column은 실행 프로그램에서 유도하며, 최소 stake 배수나 threshold를 Rust 상수로
복제하지 않는다. 첫 exit가 선택하는 다음 adjustment callback과 그 callback의
retry window도 같은 payload로 묶어 검증한다. 기존 스키마만 보존된 legacy 실행을
사용하고, 새 스키마에서는 이 프로그램이 Native primary다.

System-v3 adjustment의 `system-adjustment-program-v2`는 각 Grind level의
cluster-maximum state를 소스에서 직접 선언한다. active AST에 stake/rate read와
write가 모두 있으면 두 custom-data key를 보존한다. read, write, helper argument와
maximum binding이 모두 사라졌으면 명시적인 absent pair를 기록하고 Rust는 해당
custom-data를 읽거나 쓰지 않는다. partial pair, 잔여 argument, 이름이 바뀐 key,
알 수 없는 level 또는 state 없는 maximum binding은 컴파일이나 validation에서
차단한다. schema `0.23.0`–`0.30.0`의 program-v1은 sealed evidence replay를 위해
두 key를 필수로 유지하고, 현재 schema `0.31.0`만 program-v2를 사용한다.

## Upstream 감시

호환성 workflow는 4시간마다 NFI upstream SHA, 호환성 엔진 commit, pinned Freqtrade
image digest, semantic-profile fingerprint를 함께 확인한다. 네 값이 모두 같으면 즉시
종료하고, 어느 하나라도 바뀌면 같은 NFI 소스도 다시 검사한다.
수동 실행의 `force=true`는 같은 identity도 재검사한다. GitHub 예약 실행은
queue 사정에 따라 지연될 수 있으므로 4시간은 실시간 SLA가 아니라 검사 주기다.

검사가 필요하면 Spot/Futures 정적 검사, AST/IR diff와 변경경로 표적검증을 서로
독립 실행한다. hosted canary가 두 mode의 schema, source, qualification, 자동 분류와
네 identity를 원자적으로 검증하면 `compatibility-ledger`의
`checks/<upstream>/<engine>/<freqtrade>/<semantic-profile>/runs/<run-attempt>`에 compact
JSON을 추가하고 latest observation을 전진시킨다. Native 실행으로 분류된 mode만
changed-target ledger와 sealed promotion proof를 요구한다. 새 의미가 아직
`official_only`이면 Native 승격 proof 없이도 blocked product status, hosted canary,
호환성 issue와 관찰 ledger를 남긴다. `required_status_passed=false`는 이 blocked
product status의 권위 있는 값이며, 검증된 discovery 진행상황을 게시하지 못하게
하는 자동화 실패가 아니다. Native 승격은 여전히 두 mode의 독립 exact proof가
모두 있어야 한다.

대용량 임시 trace는 업로드하지 않으며 compact JSON artifact만 30일 보존한다.
한 mode라도 누락되거나 다운로드·빌드·권한·artifact 같은 자동화가 실패하면 latest
observation을 전진시키지 않아 다음 주기에 재시도한다. 전략 blocker는
`nfi-compatibility`, 자동화 장애는 `nfi-automation-health`로 분리한다. 호환성
issue는 하나만 열어 두고 네 identity, mode별 route/review kind, blocker,
missing-target 수와 workflow artifact link를 canonical blocker fingerprint로
갱신한다. blocker가 복구되면 해당 issue를 자동으로 닫는다.

매 업데이트마다 5년 인증을 다시 실행하지 않는다.

- `latest_checked`: 안전하게 분석·컴파일됨
- `quick_verified`: 변경 branch의 공식 full-state 증거 통과
- `release_certified`: 공개 성능 또는 5년 인증을 새로 주장할 때만 생성

Native가 아직 모르는 새 동작도 공식 fallback으로 사용할 수 있지만, 공식
full-state parity를 통과하기 전에는 Native 지원으로 표시하지 않는다.

## Spot/Futures branch discovery

4시간 감시의 Spot 또는 Futures 표적검증에서 `TARGETED_COVERAGE_GAP`이 남으면
별도의 저속 탐색 lane이 그 identity를 이어받는다. 각 lane은
`planning/spot-discovery-policy.json` 또는
`planning/futures-discovery-policy.json`만으로 범위를 정한다. 특정 pair, 날짜,
Signal/Grind 번호, 전략 SHA 또는 예상 결과는 검색 분기가 아니다.

```bash
nfi-bte strategy discover new.py strategy-diff.json report.json \
  --class NostalgiaForInfinityX7 \
  --trading-mode futures \
  --fixtures-root benchmarks/fixtures/captured \
  --policy planning/futures-discovery-policy.json \
  --baseline-source old.py \
  --baseline-upstream-commit PREVIOUS_COMMIT \
  --upstream-commit UPSTREAM_COMMIT --engine-commit ENGINE_COMMIT \
  --profile execution-profile.json --output-dir .nfi/futures-discovery
```

request fingerprint는 upstream/engine/source/policy/target/time window를 모두
묶는다. budget 소진 시 다음 미검색 shard를 가리키는 cursor를 남기며, 같은
fingerprint에서만 재개한다. 최신 identity가 바뀌면 이전 cursor는 보존하되
재사용하지 않는다. 제거된 동작, runtime에서 관찰할 수 없는 target, tag로
위치시킬 수 없는 새 callback은 새 결과만으로 증명할 수 없으므로 검색하지 않고
공식 fallback 상태로 남긴다.

검색 hit 자체는 증거가 아니다. pair와 시간을 최소화한 뒤 이전/최신 공식과 최신
Native를 각각 다시 실행해 변경 branch 도달, trade surface exact, full-state
exact를 모두 통과한 mode별 paired fixture가 30 MiB 이하일 때만 candidate가 된다.
자동화는 allowlist된 fixture와 compact evidence만 새 branch에 넣어 Draft PR을
열고 CI를 요청한다. mode마다 자동 candidate Draft는 최대 하나만 연다. 새 immutable
identity가 오면 이전 automation-owned Draft는 superseded comment와 함께 닫고, 닫힌
PR의 commit과 review history는 그대로 보존한다. 사람이 Draft를 해제한 PR은 자동으로
닫거나 수정하지 않으며 같은 mode의 새 candidate 생성을 차단한다. 자동 승인과 자동
merge는 하지 않는다.

심층 workflow는 nightly 또는 수동으로 실행되며 새 identity가 도착하면 진행 중인
이전 실행을 취소한다. 대체 실행은 전체 identity가 일치하는 cursor만 재개하고,
cancelled 또는 superseded 실행은 cursor나 PR을 갱신하지 않는다. 외부 시장 데이터가
정책에 선언된 HTTP 상태로 차단되면 엔진 실패가 아니라
`external_data_deferred`로 기록한다. 이 상태와 `budget_exhausted`는 Native exact
증거가 아니지만 schema-valid 검색 결과이므로 다음 예약 실행이 이어받을 수 있다.
정책에 없는 네트워크·빌드·권한 장애는 계속 자동화 실패다.

Spot/Futures request, report, cursor와 automation decision은 authoritative identity,
diff, static report와 targeted report에서 다시 계산한 fingerprint로 한 쌍의
publication authorization에 묶는다. 둘 중 하나가 malformed, stale, cross-mode 또는
부분 게시 상태면 어느 cursor도 전진시키지 않는다. 유효한 paired publication 뒤에는
한 mode가 `exact_fixture_draft_pr`에 도달한 즉시 그 mode의 candidate를 독립적으로
준비할 수 있지만 product status는 두 mode가 모두 exact가 될 때까지 blocked다.
candidate push 직전에도 engine/upstream ref를 다시 확인한다.

request/report/cursor는 append-only ledger에 한 번만 남긴다. candidate artifact는
30일, candidate가 없는 compact workflow artifact는 1일 보존한다. 원시 candles,
cache, Docker layer와 trace는 ledger, artifact 또는 PR에 넣지 않는다. 자동화는
allowlist된 fixture와 compact evidence만 Draft PR로 만들고 CI를 요청하며 자동 승인,
merge 또는 stable 승격을 하지 않는다. 로컬 budget/보류/인프라 실패 run은
`nfi-bte clean --dry-run`에서 회수 가능 대상으로 분류된다.

이 계약은 현재 pinned Freqtrade가 실행할 수 있는 NFI 변경을 즉시 사용할 수 있게
한다. 향후 NFI가 새로운 Freqtrade API나 외부 의존성을 요구하면 watcher가 실패
위치와 blocker를 보존하며, 해당 공식 환경의 버전·digest·회귀 fixture를 검증한
엔진 업데이트 전에는 임의의 이미지나 코드를 추측 실행하지 않는다.
