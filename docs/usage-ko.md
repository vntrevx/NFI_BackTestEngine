# 사용 설명서

[English](usage.md) · [Ελληνικά](usage-el.md) · [Türkçe](usage-tr.md)

이 문서는 설치, NFI X7 첫 백테스트, 정확한 마켓 수 선택, 저장된 프로젝트 재사용, 자주 필요한 복구 명령을 설명합니다.

## 1. 실행 환경

다음 환경을 지원합니다.

- Linux x86_64 또는 ARM64
- Apple Silicon 기반 macOS
- WSL2 Linux 셸을 사용하는 Windows

현재 Binance 마켓 순위를 계산하거나 공개 캔들을 다운로드하거나 공식 Freqtrade 비교를 실행할 때 Docker가 필요합니다. Windows 네이티브 환경과 PowerShell은 지원하지 않습니다.

## 2. CLI 설치 또는 업데이트

최신 공개 릴리즈를 SHA-256 검증 후 설치합니다.

```bash
curl -LsSf https://raw.githubusercontent.com/vntrevx/NFI_BackTestEngine/main/install.sh | sh
```

새 터미널을 열고 설치 상태를 확인합니다.

```bash
nfi-bte --version
nfi-bte doctor
```

기존 설치를 최신 버전으로 업데이트합니다.

```bash
nfi-bte update
```

## 3. NFI 다운로드

작업 폴더를 만들고 공식 NFI 저장소를 복제합니다.

```bash
mkdir -p ~/nfi-backtest
cd ~/nfi-backtest
git clone --depth 1 https://github.com/iterativv/NostalgiaForInfinity.git
cd NostalgiaForInfinity
```

`nfi-bte`는 이 NFI 폴더에서 실행하십시오. 숫자로 마켓 수를 선택할 때 이 저장소의 `configs/`에 있는 최신 거래량·필터 정책을 사용합니다.

## 4. 권장 첫 실행

대화형 설정과 백테스트를 시작합니다.

```bash
nfi-bte run NostalgiaForInfinityX7.py
```

권장값인 Spot 모드, Binance 거래소, BTC 빠른 테스트, 엔진 관리 캔들 폴더, 최근 7일을 사용하려면 각 질문에서 Enter를 누릅니다.

마켓 수 질문에는 다음 값을 입력할 수 있습니다.

| 입력 | 결과 |
| --- | --- |
| `1` | BTC 빠른 테스트. 첫 실행 권장값 |
| `10`, `20`, `40`, `80`, `100` | NFI의 Binance 정책으로 순위를 계산한 정확한 개수의 현재 마켓 |
| `all` | NFI의 전체 정적 백테스트 목록 |
| `custom` | 쉼표로 구분하여 직접 입력한 목록 |

숫자 선택은 고정된 Freqtrade 이미지에서 한 번만 계산됩니다. 정렬된 심볼은 `.nfi/project.json`에 저장되므로 이후 거래량 순위가 달라져도 저장된 프로젝트는 재현 가능합니다.

## 5. 정확히 80마켓으로 실행

새 Spot 프로젝트를 최근 7일 권장 범위로 비대화형 실행합니다.

```bash
nfi-bte run NostalgiaForInfinityX7.py \
  --trading-mode spot \
  --pair-count 80 \
  --yes
```

80마켓 5년 작업은 약 39 GiB의 메모리가 필요할 수 있습니다. 긴 기간을 선택하기 전에 먼저 7일 실행을 확인하십시오.

### 기존 저장 프로젝트 교체

`.nfi/project.json`이 이미 있으면 명시적으로 다시 설정합니다. 새 출력 폴더를 사용하면 과거 1페어 결과가 재개되는 일을 막을 수 있습니다.

```bash
nfi-bte init --force NostalgiaForInfinityX7.py \
  --trading-mode spot \
  --pair-count 80 \
  --output-dir .nfi/runs/x7-80-pairs \
  --yes

nfi-bte run
```

캔들 저장소는 `.nfi/data/binance` 아래에서 공유되며 해시가 유효한 기존 다운로드는 재사용됩니다.

## 6. 페어 직접 선택

Spot 마켓마다 `--pair`를 반복합니다.

```bash
nfi-bte run NostalgiaForInfinityX7.py \
  --trading-mode spot \
  --pair BTC/USDT \
  --pair ETH/USDT \
  --timerange 20260101-20260108 \
  --output-dir .nfi/runs/x7-btc-eth \
  --yes
```

격리 Futures에서는 Futures 모드와 결제 통화 접미사가 붙은 정규 심볼을 사용합니다.

```bash
nfi-bte run NostalgiaForInfinityX7.py \
  --trading-mode futures \
  --pair BTC/USDT:USDT \
  --pair ETH/USDT:USDT \
  --timerange 20260101-20260108 \
  --output-dir .nfi/runs/x7-futures-btc-eth \
  --yes
```

Futures 마켓을 자동 선택하려면 직접 지정한 `--pair` 대신 `--pair-count 10`, `20`, `40`, `80`, `100` 중 하나를 사용합니다.

## 7. 기간 선택

`YYYYMMDD-YYYYMMDD` 형식을 사용합니다. 종료일은 포함되지 않습니다.

```bash
nfi-bte run NostalgiaForInfinityX7.py \
  --pair-count 20 \
  --timerange 20260101-20260201 \
  --yes
```

대화형 모드에서 `--timerange`를 생략하면 최근 7일 권장값을 선택할 수 있습니다. `--yes`와 함께 생략하면 최근 7일이 자동 선택됩니다.

## 8. 실행 재개와 결과 확인

설정 후 저장된 프로젝트를 실행하거나 재개합니다.

```bash
nfi-bte run
```

터미널은 작업 시작 전에 실행 폴더를 출력합니다. 완료되면 핵심 결과를 간결한 ASCII
박스로 보여 주고 결과 폴더와 보고서/내보내기 파일 이름을 출력합니다. 사람이 읽는
결과는 `report.md`이고 JSON과 CSV 파일은 기계 판독 계약으로 유지됩니다.

```text
.nfi/runs/<strategy-and-timerange>/
├── report.md
├── summary.json
├── trades.csv
├── orders.csv
├── equity.csv
├── verification.json
└── evidence/index.json
```

`report.md`는 이식 가능한 Freqtrade 스타일 ASCII 표를 사용하며 정상적인 거래 0건
결과와 실행 오류를 명확히 구분합니다. 보고서를 다시 생성하면 오래된 `report.html`을
삭제하며 CLI는 더 이상 브라우저를 열거나 열지 묻지 않습니다.

시뮬레이션을 다시 실행하지 않고 Markdown 보고서와 기계 판독 내보내기를 다시
생성하려면 다음을 실행하십시오.

```bash
nfi-bte report .nfi/runs/<strategy-and-timerange>
```

엔진은 해시가 유효한 완료 단계만 재개합니다. 페어, 기간, 모드 또는 다른 실행 입력을 의도적으로 변경할 때는 다른 `--output-dir`을 사용하십시오.

## 9. Native 시뮬레이션 없이 데이터만 준비

페어 순위 계산, 공개 캔들 다운로드, 입력 준비까지만 수행합니다.

```bash
nfi-bte run NostalgiaForInfinityX7.py \
  --pair-count 20 \
  --prepare-only \
  --yes
```

이후 `nfi-bte run`을 실행하면 저장된 프로젝트를 계속 진행합니다.

## 10. 자주 쓰는 복구 명령

설정과 실행 옵션을 모두 확인합니다.

```bash
nfi-bte run --help
nfi-bte init --help
```

저장된 프로젝트가 이미 있다는 메시지가 나오면 다음 명령으로 계속 실행합니다.

```bash
nfi-bte run
```

설정을 의도적으로 교체하려면 다음 명령을 사용합니다.

```bash
nfi-bte init --force NostalgiaForInfinityX7.py
```

Docker 또는 Binance가 일시적으로 응답하지 않으면 같은 명령을 다시 실행하십시오. 페어 순위 계산 실패의 기술 정보는 `.nfi/pair-selection-error.log`에 저장됩니다. 캔들 다운로드 재시도를 모두 소진하면 정확한 `download-error.log` 경로가 출력됩니다. 부분적으로 완료된 유효한 캔들 다운로드는 재사용됩니다.
