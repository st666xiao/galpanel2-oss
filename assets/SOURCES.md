# SOURCES：GalPanel 2 生成素材

由 Qwen-Image 2.1 Turbo 生成（8 步、CFG 1.0）。原有素材记录声明许可为 Qwen Research License、仅个人自用；此声明原样保留，不纳入项目代码的 MIT 许可。发布或再分发素材前应核实生成模型和素材适用的许可范围。

下表记录生成批次、seed 与后处理，不包含生成设备、本机目录或未随仓库提供的脚本。

| 素材 | 原图 | 后处理 |
|---|---|---|
| `washi-day` | `gp-washi-day-s29`（seed 29） | 缩到 768，偏移羽化成无缝平铺 |
| `washi-night` | `gp-washi-night-s11`（seed 11） | 同上 |
| `aizome-cloth` | `gp-aizome-s11`（seed 11） | 无 |
| `brush-rule` | `gp-brush-rule-s29`（seed 29） | 灰度转 alpha，墨色 #1F1B16 |
| `empty-scan` | `gp-empty-scan-s29`（seed 29） | 同上 |
| `empty-shelf` | `gp-empty-shelf-s29`（seed 29） | 同上 |
| `missing-file` | `gp-missing-file-s11`（seed 11） | 同上 |
| `seal-grain` | `gp-seal-grain-s29`（seed 29） | 裁出印面，按印泥浓度转 alpha，朱 #C53D2B |
| `frontis-kura` | `gp-frontis-kura-s11`（seed 11） | 无 |
| `suminagashi-a` | `gp2-suminagashi-a-s29`（seed 29） | 无 |
| `suminagashi-b` | `gp2-suminagashi-b-s11`（seed 11） | 无 |
| `jacket-01` | `gp2-jacket-01-s29`（seed 29） | 裁去 4% 边、饱和度 ×0.82、缩到 320×464 |
| `jacket-02` | `gp2-jacket-02-s29`（seed 29） | 裁去 4% 边、饱和度 ×0.82、缩到 320×464 |
| `jacket-03` | `gp2-jacket-03-s29`（seed 29） | 裁去 4% 边、饱和度 ×0.82、缩到 320×464 |
| `jacket-04` | `gp2-jacket-04-s29`（seed 29） | 裁去 4% 边、饱和度 ×0.82、缩到 320×464 |
| `jacket-05` | `gp2-jacket-05-s29`（seed 29） | 裁去 4% 边、饱和度 ×0.82、缩到 320×464 |
| `jacket-06` | `gp2-jacket-06-s29`（seed 29） | 裁去 4% 边、饱和度 ×0.82、缩到 320×464 |
| `jacket-07` | `gp2-jacket-07-s29`（seed 29） | 裁去 4% 边、饱和度 ×0.82、缩到 320×464 |
| `jacket-08` | `gp2-jacket-08-s29`（seed 29） | 裁去 4% 边、饱和度 ×0.82、缩到 320×464 |

## 第三轮（和风、示意封面）


| 素材 | 原图 | 后处理 |
|---|---|---|
| `cover-01`～`10` | `gp3-cover-NN-s11` | 缩到 480×696。虚构作品的示意封面，只给 mockup 用，不进程序 |
| `kinpaku` | `gp3-kinpaku-s11` | 缩到 512 |
| `kinran` | `gp3-kinran-s29` | 无 |
| `donsu` | `gp3-kinran-b-s11` | 无 |
| `hinoki` | `gp3-hinoki-s11` | 无 |
| `kasumi` | `gp3-kasumi-s11` | 亮度转 alpha |

## 第四轮（gp4-，二次元方向）

- `shiori.png`：看板娘 栞，gp4-mascot-s47，白底抠图
- `shiori-puzzled.png` / `shiori-happy.png`：以 s47 为输入用 `--image` 编辑表情（seed 7）
- `petals.png`：gp4-petals-s11，按亮度转 alpha
- `title-bg.jpg`：gp4-titlebg-s29，标题画面背景

## 第五轮（gp5-，栞重设计）

- `s2-base.png`：gp5-base-s29（圆眼镜、栞绳流苏、襷、大正围裙、腰挂朱印、耳后笔、脸上墨痕）
- `s2-wave/loupe/stamp/carry/chibi.png`：以 s29 为输入用 `--image` 编辑姿势（seed 7）
