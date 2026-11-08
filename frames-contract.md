# /frames 视觉理解契约（scene 分区版 v1.1）

> 2026-11-08 主人拍板"视觉桥一次开发复用"后，OS 主持汇总三方（tablet-pet/eye-server、minecraft/MC 捕获、stardew-host/星露谷捕获）口径钉死。
> 实现方：tablet-pet（eye-server 现有 `/frames` 扩展分区）；消费方：minecraft（`/look`）、stardew-host（星露谷捕获）。
> 改本契约先在 RN 群里说，三方都点头再动代码。
>
> **v1.1（2026-11-08 晚）**：OS 主持点头三条实现方附注——`meta.mime`（PNG 直传）、`note` 透传进分区 notes、传输层失败用 HTTP 码分层。变更理由见 §7。

## 1. 端点与请求

`POST /frames`（eye-server，Bearer 认证），body：

```json
{
  "frames": ["<jpeg/png base64>", "..."],
  "meta": { "app_type": "pet | mc | stardew", "window": "<场景内窗口名，可空>",
            "note": "<调用方备注，可空>", "mime": "jpeg | png（可选，默认 jpeg）" }
}
```

- 每请求 **≤3 帧**；单帧 **≤800KB（解码后字节）**（软目标 100–300KB；tablet-pet 原话"≤800w"，实现按 800KB 钉死）
- `meta.mime` 可选：**默认 jpeg**；`png` 直传（MC 的 prismarine-viewer 原生渲染就是 PNG，不让消费方转码）。原 v1 写死 jpeg，v1.1 放开。
- `app_type` 三分区：`pet`（桌宠，现状零改动）/ `mc`（prismarine-viewer 渲染帧）/ `stardew`（pc_body PrintWindow 窗口帧）
- 请求头带 `X-Scene: <app_type>`（归因用）。**与 body `app_type` 必须一致，双向校验**：头 `mc` + body `pet`、或头 `pet` + body `mc` 都是 400 `scene-mismatch`。老 pet 客户端的 `app_type` 是 `game/video/other`，一律按 pet 处理（零改动）。

## 2. 响应（两值契约，mc/stardew 场景**没有 SKIP**）

```json
{"ok": true,  "desc": "<按场景 prompt 的一句话描述>"}
{"ok": false, "reason": "unreadable | too-dark | no-frame"}
```

- mc/stardew 场景**禁止返回 SKIP**：她需要的是"没看清"（回退 `/state`+`/near`），不是"没东西"。
- VLM 延迟按 2–5s 计：**调用方不得在动作循环里同步等待**（异步发或走 `/wait`）。
- 实现口径：模型输出含 `[SKIP]` 一律落成 `{"ok":false,"reason":"unreadable"}`；中置标记后面还有正文时，剥离标记照写 desc 并在行里带 `skip_marker:true`。
- 成功响应可带辅助字段（调用方可忽略）：`scene / lane / tokens / ms / frames`。

**传输层失败不走两值，走 HTTP 码**（全部立即返回、不排队）：

| 码 | 含义 | body |
| --- | --- | --- |
| 400 | 形状/场景错：`no-frame` `bad-frames` `too-many-frames` `frame-too-large` `scene-mismatch` `unknown-scene` | `{ok:false, scene?, reason?, error?, ...}` |
| 401 | 凭据：`unauthorized`（token 与场景不匹配）/ `scene-disabled`（该场景没发 token） | `{ok:false, error, scene}` |
| 413 | 整包超 1.5MB | `{error}` |
| 429 | 限频（每场景 ≤1 次/2s） | `{ok:false, reason:"rate-limited", retry_after_s}` |
| 502 | 模型调用失败（两条车道都失败） | `{ok:false, reason:"vision-failed", detail}` |
| 503 | 暂停 / 日额度耗尽 / 场景 prompt 缺失 | `{ok:false, reason:"paused|daily-limit|prompt-missing"}` |

分诊口径（消费方照这个做）：**200 + `ok:false` = 视觉上没看清**（回退 `/state`+`/near`）；**429/503 = 额度或暂停**（退避，不要重试轰炸）；**400 = 调用方形状错**；**401 = token/场景错**。

## 3. 分区闸（每 app_type 独立，互不误杀）

| 状态 | 说明 |
| --- | --- |
| `day_bytes` / `day_calls` | 每场景独立日额度（mc、stardew ≤200 次/天） |
| `paused` / 异常滑窗 | 每场景独立暂停，MC/星露谷突发不连坐桌宠 |
| 去重/节流 | **mc/stardew 不套 governor、不套 dup**（"盖房前看一眼"不是复读） |
| notes 文件 | pet=notes.jsonl（现状）；mc/stardew 各自独立文件（`notes-<scene>.jsonl`），不混流 |
| 调用方备注 | body `note` 落进该场景 notes 行（`note` 字段）；ledger 不存（防膨胀） |

- 限频：每场景 **≤1 请求/2s**。
- 池车道提醒：VLM 调用与鱼鱼/OS 主持共用单账号并发 3——突发连拍前先看她回合忙不忙。

## 4. 认证与 prompt 归属

- token 分文件：`eye-server.token`（pet，现状）/ `eye-server.mc.token` / `eye-server.stardew.token`。
- prompt 归属：pet→tablet-pet（不动）；**mc→minecraft 主持**；**stardew→stardew-host**。实现方不改场景判据文本。
  落地方式：各场景主持在服务器 `~/pc-body/prompts/<scene>.txt` 写判据文本，eye-server 只读；文件不在 → 503 `prompt-missing`（**不拿 pet 的判据顶替**）。
- 帧落盘归调用方（mc `/look` 返回 `savedTo`；星露谷同款）——eye-server 只回 desc，不替身体存图。

## 5. 捕获层分工（薄适配器，理解层只有这一份）

| 身体 | 捕获 | 负责 |
| --- | --- | --- |
| 桌宠 | PrintWindow（现有） | tablet-pet |
| MC | prismarine-viewer 无头渲染（bot 视角，不用开游戏） | minecraft |
| 星露谷 | pc_body `/clip` 窗口截图管道 | stardew-host |

## 6. 验收口径

- pet 场景回归：现有 notes 流零变化（金标准=改动前后 24h 对账）。
- mc/stardew 各一条端到端：出帧 → desc 落回 → 调用方落盘 → 限频/日额度真实生效（超限返回失败而非排队）。

## 7. 变更记录

**v1.1（2026-11-08）** —— 三条都来自实现方（tablet-pet）在 #85 里的附注，OS 主持点头：

1. **`meta.mime` 可选（默认 jpeg，`png` 直传）**：v1 写死 jpeg，但 MC 的 prismarine-viewer 原生出 PNG，强制转码是平白加一跳。
2. **`note` 透传进分区 notes 行**：body 本来就有 `note`，v1 没说它去哪；现约定落进该场景 notes（ledger 不存）。
3. **传输层失败用 HTTP 码分层**：v1 §2 的两值只覆盖"看懂了/没看清"；凭据/形状/限频/暂停/模型失败用 401·400·413·429·503·502，并给 `retry_after_s`。与 §6"超限返回失败而非排队"一致。

**v1（2026-11-08）** —— 首版，三方口径钉死。
