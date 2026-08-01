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
registry, ledger, terminal, HTML에서 독립 상태로 표시된다.

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

## Managed exit의 단계적 Native 전환

8개 managed-long route의 `custom_exit` 분기 순서와 재귀 any/all matcher, 수익
basis와 gate, mode name, pure decision 호출 순서는 이제
`managed-exit-program-v1` 데이터로 컴파일된다. 복합 rebuy/rapid/scalp tag도
상수값별 Rust 분기 없이 같은 matcher evaluator가 처리한다. 같은 callback에서
기존 stateful 경로도 독립 평가하며 routing 또는 decision 결과가 다르면 즉시
중단한다.

아직 stop, profit-target cache와 quick/rapid inline state는 기존 경로가 결과를 소유한다.
따라서 이 단계는 자동 인식 범위를 넓힌 shadow 증명이지 generic 기본 경로 승격은
아니다. 남은 state와 특수 long/short route까지 branch-reaching exact가 끝난 뒤에만
legacy hash gate를 제거한다.

## Upstream 감시

호환성 workflow는 4시간마다 upstream SHA와 호환성 엔진 commit을 함께 확인한다.
둘 다 같으면 즉시 종료하고, upstream이 같아도 엔진이 개선됐으면 다시 검사한다.
수동 실행의 `force=true`는 같은 identity도 재검사한다. GitHub 예약 실행은
queue 사정에 따라 지연될 수 있으므로 4시간은 실시간 SLA가 아니라 검사 주기다.

검사가 필요하면 Spot/Futures 정적 검사, AST/IR diff와 변경경로 표적검증을
실행한다. 성공한 실행만 `compatibility-ledger`의
`checks/<upstream>/<engine>/runs/<run-attempt>`에 compact JSON으로 추가하고,
대용량 임시 trace는 업로드하지 않는다. compact JSON artifact만 30일 보존한다.
실패한 자동화는 identity를 전진시키지 않아 다음 주기에 재시도한다.

전략 blocker는 `nfi-compatibility`, 다운로드·빌드·권한·artifact 장애는
`nfi-automation-health`로 분리한다. 같은 canonical fingerprint는 issue와 알림을
중복 생성하지 않으며, 복구 또는 새로운 blocker가 확인되면 기존 issue 상태를
자동 조정한다.

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
열고 CI를 요청한다. 자동 승인과 자동 merge는 하지 않는다.

심층 workflow는 nightly 또는 수동으로 실행되고 동시 실행은 하나뿐이다. 외부
시장 데이터가 정책에 선언된 HTTP 상태로 차단되면 엔진 실패가 아니라
`external_data_deferred`로 기록한다. 이 상태는 Native exact 증거가 아니며 cursor를
전진시키지 않는다. 같은 upstream/engine/Freqtrade identity의 예약 실행은 저장된
compact 결과를 재사용해 빌드와 외부 요청을 반복하지 않는다. identity가 바뀌거나
수동 실행에서 `retry_deferred`를 선택한 경우에만 다시 요청한다. 정책에 없는
네트워크·빌드·권한 장애는 계속 실패한다.

request/report/cursor는 append-only ledger에 한 번만 남긴다. candidate artifact는
30일, candidate가 없는 compact workflow artifact는 1일 보존한다. 원시 candles,
cache, Docker layer와 trace는 ledger, artifact 또는 PR에 넣지 않는다. 로컬
budget/보류/인프라 실패 run은 `nfi-bte clean --dry-run`에서 회수 가능 대상으로
분류된다.

이 계약은 현재 pinned Freqtrade가 실행할 수 있는 NFI 변경을 즉시 사용할 수 있게
한다. 향후 NFI가 새로운 Freqtrade API나 외부 의존성을 요구하면 watcher가 실패
위치와 blocker를 보존하며, 해당 공식 환경의 버전·digest·회귀 fixture를 검증한
엔진 업데이트 전에는 임의의 이미지나 코드를 추측 실행하지 않는다.
