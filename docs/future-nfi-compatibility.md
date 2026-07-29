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
```

변경 분석은 Signal/tag, callback, dataframe column, custom trade state, Grind level과
범용 opcode를 추출한다. 기존 명령 조합으로 표현되는 변경은 전략 소스에서
컴파일한다. 새로운 callback, 동적 키, 무제한 반복, 모호한 stake 방향 또는 알 수
없는 상태 전이는 정확한 소스 위치와 함께 fail-closed한다.

상태 머신 VM은 source order, step limit, typed custom state, wallet/trade/order read,
추가 진입, 부분 청산과 exit를 실행한다. 실패한 실행은 custom state를 원자적으로
rollback한다. Signal 번호와 Grind 단계 수는 opcode가 아니라 IR 데이터다.

`quick_verified` 승격에는 서로 다른 legacy/candidate 실행, 같은 sealed workload,
변경 branch 도달, trade surface exact, full-state exact가 모두 필요하다. 같은
artifact를 양쪽 증거로 재사용할 수 없다.

## Upstream 감시

호환성 workflow는 4시간마다 upstream SHA를 확인하고 동일 SHA이면 즉시 종료한다.
변경이 있으면 Spot/Futures 정적 검사와 AST/IR diff를 만들고 append-only
`compatibility-ledger` branch에 SHA별 증거를 저장한다. blocker fingerprint가 같은
실패는 issue와 알림을 중복 생성하지 않으며, 복구 또는 새로운 blocker가 확인되면
기존 issue 상태를 조정한다.

매 업데이트마다 5년 인증을 다시 실행하지 않는다.

- `latest_checked`: 안전하게 분석·컴파일됨
- `quick_verified`: 변경 branch의 공식 full-state 증거 통과
- `release_certified`: 공개 성능 또는 5년 인증을 새로 주장할 때만 생성

Native가 아직 모르는 새 동작도 공식 fallback으로 사용할 수 있지만, 공식
full-state parity를 통과하기 전에는 Native 지원으로 표시하지 않는다.
