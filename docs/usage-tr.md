# Kullanım Kılavuzu

[English](usage.md) · [한국어](usage-ko.md) · [Ελληνικά](usage-el.md)

Bu kılavuz kurulum, ilk NFI X7 backtest'i, tam piyasa sayısı seçimi, kayıtlı projenin yeniden kullanılması ve yaygın kurtarma komutlarını açıklar.

## 1. Gereksinimler

Aşağıdaki ortamlar desteklenir:

- Linux x86_64 veya ARM64
- Apple Silicon üzerinde macOS
- WSL2 Linux kabuğu üzerinden Windows

Motorun güncel Binance piyasalarını sıralaması, herkese açık mum verilerini indirmesi veya resmi Freqtrade karşılaştırması çalıştırması gerektiğinde Docker zorunludur. Yerel Windows ve PowerShell desteklenmez.

## 2. CLI'yi kurma veya güncelleme

En son herkese açık sürümü SHA-256 doğrulamasıyla kurun:

```bash
curl -LsSf https://raw.githubusercontent.com/vntrevx/NFI_BackTestEngine/main/install.sh | sh
```

Yeni bir terminal açın ve kurulumu doğrulayın:

```bash
nfi-bte --version
nfi-bte doctor
```

Mevcut kurulumu güncelleyin:

```bash
nfi-bte update
```

## 3. NFI'yi indirme

Bir çalışma dizini oluşturun ve resmi NFI deposunu klonlayın:

```bash
mkdir -p ~/nfi-backtest
cd ~/nfi-backtest
git clone --depth 1 https://github.com/iterativv/NostalgiaForInfinity.git
cd NostalgiaForInfinity
```

`nfi-bte` komutunu bu NFI dizininden çalıştırın. Sayısal piyasa seçimi, bu deponun `configs/` dizinindeki güncel hacim ve filtre politikasını kullanır.

## 4. Önerilen ilk çalıştırma

Etkileşimli kurulumu ve backtest'i başlatın:

```bash
nfi-bte run NostalgiaForInfinityX7.py
```

Önerilen Spot modu, Binance borsası, BTC hızlı testi, motor tarafından yönetilen mum dizini ve son yedi tam günü kabul etmek için her soruda Enter'a basın.

Piyasa sayısı sorusu şu değerleri kabul eder:

| Girdi | Sonuç |
| --- | --- |
| `1` | İlk çalıştırma için önerilen BTC hızlı testi |
| `10`, `20`, `40`, `80`, `100` | NFI'nin Binance politikasına göre sıralanan tam olarak bu sayıda güncel piyasa |
| `all` | NFI'nin tam statik backtest listesi |
| `custom` | Virgülle ayrılarak elle girilen liste |

Sayısal seçimler sabitlenmiş Freqtrade imajıyla bir kez hesaplanır. Sıralı semboller `.nfi/project.json` dosyasına kaydedilir; böylece borsa hacimleri daha sonra değişse bile kayıtlı proje yeniden üretilebilir kalır.

## 5. Tam olarak 80 piyasayla çalıştırma

Önerilen yedi günlük dönemi kullanan yeni ve etkileşimsiz bir Spot projesi için:

```bash
nfi-bte run NostalgiaForInfinityX7.py \
  --trading-mode spot \
  --pair-count 80 \
  --yes
```

80 piyasalı beş yıllık bir iş yükü yaklaşık 39 GiB bellek gerektirebilir. Uzun bir zaman aralığı seçmeden önce yedi günlük çalıştırmayı doğrulayın.

### Mevcut kayıtlı projeyi değiştirme

`.nfi/project.json` zaten varsa projeyi açıkça yeniden yapılandırın. Yeni bir çıktı dizini, eski tek piyasalı sonuçların devam ettirilmesini önler:

```bash
nfi-bte init --force NostalgiaForInfinityX7.py \
  --trading-mode spot \
  --pair-count 80 \
  --output-dir .nfi/runs/x7-80-pairs \
  --yes

nfi-bte run
```

Mum verileri `.nfi/data/binance` altında ortak kullanılmaya devam eder. Hash'i geçerli mevcut indirmeler yeniden kullanılabilir.

## 6. Piyasaları açıkça seçme

Her Spot piyasası için `--pair` seçeneğini tekrarlayın:

```bash
nfi-bte run NostalgiaForInfinityX7.py \
  --trading-mode spot \
  --pair BTC/USDT \
  --pair ETH/USDT \
  --timerange 20260101-20260108 \
  --output-dir .nfi/runs/x7-btc-eth \
  --yes
```

İzole Futures için Futures modunu ve uzlaşma para birimi son ekine sahip standart sembolleri kullanın:

```bash
nfi-bte run NostalgiaForInfinityX7.py \
  --trading-mode futures \
  --pair BTC/USDT:USDT \
  --pair ETH/USDT:USDT \
  --timerange 20260101-20260108 \
  --output-dir .nfi/runs/x7-futures-btc-eth \
  --yes
```

Otomatik Futures seçimi için açık `--pair` seçeneklerini `--pair-count 10`, `20`, `40`, `80` veya `100` ile değiştirin.

## 7. Zaman aralığı seçme

`YYYYMMDD-YYYYMMDD` biçimini kullanın. Bitiş tarihi dahil değildir:

```bash
nfi-bte run NostalgiaForInfinityX7.py \
  --pair-count 20 \
  --timerange 20260101-20260201 \
  --yes
```

Etkileşimli modda `--timerange` atlanırsa son yedi günlük önerilen dönem sunulur. `--yes` ile birlikte atlanırsa bu dönem otomatik seçilir.

## 8. Devam etme ve sonuçları inceleme

Kurulumdan sonra kayıtlı projeyi çalıştırın veya devam ettirin:

```bash
nfi-bte run
```

Terminal, iş başlamadan önce çalıştırma dizinini gösterir. Tamamlandığında kısa bir
ASCII özeti, sonuç dizinini ve rapor/dışa aktarma dosyalarının adlarını yazdırır.
İnsan tarafından okunabilen sonuç `report.md` dosyasıdır; JSON ve CSV dosyaları
makine tarafından okunabilen sözleşmeler olarak kalır:

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

`report.md`, taşınabilir Freqtrade tarzı ASCII tablolar kullanır ve geçerli bir
sıfır işlemlik sonucu yürütme hatasından açıkça ayırır. Rapor yeniden oluşturulduğunda
eski `report.html` silinir; CLI artık tarayıcı açmaz veya açmak için onay istemez.

Simülasyonu yeniden çalıştırmadan Markdown raporunu ve makine tarafından okunabilen
dışa aktarımları yeniden oluşturmak için:

```bash
nfi-bte report .nfi/runs/<strategy-and-timerange>
```

Motor yalnızca hash'i geçerli tamamlanmış aşamalara devam eder. Piyasaları, zaman aralığını, modu veya diğer çalıştırma girdilerini bilerek değiştirdiğinizde farklı bir `--output-dir` kullanın.

## 9. Native simülasyonu başlatmadan veri hazırlama

Backtest'i çalıştırmadan piyasaları sıralamak, gerekli herkese açık mumları indirmek ve girdileri hazırlamak için:

```bash
nfi-bte run NostalgiaForInfinityX7.py \
  --pair-count 20 \
  --prepare-only \
  --yes
```

Kayıtlı projeye devam etmek için daha sonra `nfi-bte run` komutunu çalıştırın.

## 10. Yaygın kurtarma komutları

Tüm kurulum ve çalıştırma seçeneklerini gösterin:

```bash
nfi-bte run --help
nfi-bte init --help
```

CLI kayıtlı bir projenin zaten var olduğunu bildirirse şu komutla devam edin:

```bash
nfi-bte run
```

veya kurulumu bilerek değiştirin:

```bash
nfi-bte init --force NostalgiaForInfinityX7.py
```

Docker veya Binance geçici olarak kullanılamıyorsa aynı komutu yeniden çalıştırın. Piyasa sıralama hatalarının teknik ayrıntıları `.nfi/pair-selection-error.log` dosyasında saklanır. Mum indirme denemeleri tükendiğinde tam `download-error.log` yolu yazdırılır. Kısmen tamamlanmış geçerli mum indirmeleri yeniden kullanılabilir.
