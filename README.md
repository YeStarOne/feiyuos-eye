# feiyu-eye · 肥鱼 OS 的眼睛（eye-server）

> 鱼鱼的视觉理解服务：截图/帧进来，一句中文场景描述出去。
> 本仓是 [feiyu-os](https://github.com/YeStarOne/feiyu-os) 的车道仓——桌宠、Minecraft、星露谷三个"眼睛"共用这一份理解层。

## 它做什么

```
帧（JPEG/PNG，解码后 ≤800KB，≤3 帧/请求）
  → POST /frames  (Bearer token, X-Scene 归因)
  → 按 meta.app_type 三分区：pet(桌宠) / mc(prismarine-viewer 渲染) / stardew(窗口截图)
  → 每场景独立闸（日额度/暂停/anomaly 滑窗）+ 独立 notes 文件
  → {"ok":true,"desc":"…"} 或 {"ok":false,"reason":"unreadable|too-dark|no-frame"}
```

- **pet**：沿用原版全闸（governor/去重/SKIP），老客户端零改动
- **mc / stardew**：无 SKIP、不去重（"盖房前看一眼"不是复读）——失败只许两值契约
- 契约全文：[frames-contract.md](frames-contract.md)（v1.1，2026-11-08 定稿）

## 安装

1. Python 3.10+，依赖见 `eye_server.py` 头部注释（requests/Pillow 等）。
2. 视觉模型端点与 Bearer token：配置在 eye_server 侧（token 文件 `~/.dsh/eye-server.token` 起家；mc/stardew 场景各用独立 token 文件）。
3. 场景判据文件放 `prompts/`（`mc.txt` = Minecraft 场景；stardew 场景由星露谷车道贡献）。
4. 跑：`python eye_server.py`（systemd user unit 部署在云端服务器，绑 loopback）。

## 自测

```bash
python selftest_scene.py     # 40/40 场景用例 + 阳性对照（改坏必转红）
python tools/notes_audit.py --selftest                 # 对账工具自检（合成数据，已知答案）
python tools/notes_audit.py ~/pc-body/notes.jsonl --date 2026-11-08 --split 12:48,19:41
```

`notes_audit.py` 是 24h 金标准对账的**可重复口径**：分母先行（每段先报 n），
再给 kind/lane/帧数/token/SKIP 泄漏/空 window/近重复；硬不变量破了才 exit 1。
⚠️ 它**不做门槛判定**，只报计数——门槛在 `frames-contract.md` §6。

## 消费方

| 身体 | 捕获 | 仓库 |
| --- | --- | --- |
| 桌宠 | PrintWindow（pc_body /clip） | [feiyu-os code/pc](https://github.com/YeStarOne/feiyu-os) |
| Minecraft | prismarine-viewer 无头渲染 | [feiyuos-minecraft](https://github.com/YeStarOne/feiyuos-minecraft) |
| 星露谷 | pc_body /clip 窗口帧 | [feiyuos-stardew-ai-farmer](https://github.com/YeStarOne/feiyuos-stardew-ai-farmer) |

## 红线

- 只绑 loopback/Tailscale；token 分文件不混用；pet 场景行为改动前后必须跑 24h notes 对账。
- 任何改 prompts/ 或闸逻辑的 PR，先在群里说一声（三个消费方都挂着）。

## License

MIT（与 feiyu-os 一致）
