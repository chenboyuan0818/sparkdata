# 部署指南

目标：给面试官一个**点开就能用**的公开链接。

已提供 `Dockerfile`（通用）、`Procfile` 与 `runtime.txt`（平台自动识别用）。

> ⚠️ **前提**：代码已推送到 GitHub（`https://github.com/chenboyuan0818/sparkdata`）。
> 本文档的方案基于这一点。**不要在 `demo/` 目录里执行 `git init`** ——
> 项目根目录已经是一个 git 仓库，在子目录再建仓库会造成嵌套，后续难以维护。

---

## 方案 A：Render（推荐，全程网页操作）

因为已经有 GitHub 仓库，这条路**一条命令都不用敲**。

### 步骤

**1.** 访问 https://render.com ，用 GitHub 账号登录

**2.** `New` → `Web Service` → 选择 `chenboyuan0818/sparkdata` 仓库

**3.** 填写配置：

| 字段 | 值 |
|---|---|
| Name | `skill-platform`（会成为域名的一部分） |
| **Root Directory** | **`demo`** ← 关键，代码在子目录里 |
| Language / Runtime | `Docker` |
| Instance Type | `Free` |

**4.** 展开 `Advanced` → `Add Environment Variable`，加三条：

| Key | Value | 说明 |
|---|---|---|
| `LLM_API_KEY` | `sk-ant-...` | 你的真实 Key |
| `LLM_PROVIDER` | `anthropic` | |
| `LLM_MODEL` | `claude-opus-5` | |

**5.** `Create Web Service` → 等待 3-5 分钟构建

Render 会自动注入 `$PORT`，Dockerfile 已经处理好了。

### 唯一的缺点：冷启动

免费版在 15 分钟无访问后会休眠，**再次访问要等 50 秒以上**。

应对办法：**把链接发给面试官之前，自己先访问一次把它唤醒。** 另外可以在提交材料里附一句"首次打开可能需要等待约一分钟唤醒"，这属于诚实说明，不减分。

---

## 方案 B：Hugging Face Spaces（唤醒更快，但要敲命令）

唤醒只要十几秒，体验更好，代价是要多做两步。

### 步骤

**1. 新建 Space**

https://huggingface.co/new-space

| 字段 | 填什么 |
|---|---|
| Space name | `skill-platform` |
| **Space SDK** | **`Docker` → `Blank`** ← 别选 Gradio |
| Hardware | `CPU basic · Free` |
| Visibility | `Public` |

**2. 配置环境变量**（推代码之前做）

Space → `Settings` → `Variables and secrets`

- `New secret`：`LLM_API_KEY` = 你的 Key
- `New variable`：`LLM_PROVIDER` = `anthropic`
- `New variable`：`LLM_MODEL` = `claude-opus-5`

> ⚠️ Key 必须走 **Secret**。Space 是公开仓库，任何人都能看到文件内容。

**3. 给 `demo/README.md` 顶部加 HF 配置**

HF 需要读 README 开头的一段 YAML 来识别 Space 配置。在 `demo/README.md` **第一行之前**插入：

```yaml
---
title: 企业岗位经验 Skill 生成平台
emoji: 🧩
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---
```

改完提交到 GitHub：

```bash
cd "/Users/yuanchenbo/Desktop/数花科技_面试题" && git add . && git commit -m "补充 HF Space 配置" && git push
```

**4. 用 subtree 推送 `demo/` 子目录**

这是关键一步。HF 要求 `Dockerfile` 在仓库根目录，而我们的在 `demo/` 里。
`git subtree` 可以把子目录的内容作为根目录推送出去，**不需要新建仓库**：

```bash
cd "/Users/yuanchenbo/Desktop/数花科技_面试题" && git remote add space https://huggingface.co/spaces/你的HF用户名/skill-platform && git subtree push --prefix demo space main
```

推送时要求输入用户名和密码，**密码填 Access Token**：
https://huggingface.co/settings/tokens → New token → 权限选 `Write`

> 以后 `demo/` 有更新时，重新执行一次 `git subtree push --prefix demo space main` 即可，
> 不用再 `git remote add`。

---

## 该选哪个

| | Render | HF Spaces |
|---|---|---|
| 操作方式 | 纯网页 | 要敲 subtree 命令 |
| 唤醒耗时 | **50 秒以上** | 十几秒 |
| 是否需要改 README | 否 | 是（加一段 YAML，GitHub 上会渲染成小表格） |
| 免费 | ✅ | ✅ |

**建议：先用 Render 跑通**（省事，且能验证 Dockerfile 没问题）。
如果觉得冷启动实在影响体验，再花十分钟迁到 HF Spaces。

---

## 部署后必做的检查

```bash
# 换成你的实际地址
curl -s https://你的地址/api/status
```

期望看到 `"mock_mode": false` 和 `"model": "claude-opus-5"`。

**如果 `mock_mode` 是 `true`**，说明 Key 没被读到：

- 确认变量名是 `LLM_API_KEY`（大小写敏感）
- 确认 `LLM_PROVIDER` 设成了 `anthropic`
- HF Spaces 改完 Secret 需要 `Settings → Factory rebuild` 才生效
- Render 改完环境变量会自动重新部署，等它跑完

然后在浏览器里手动跑一遍完整流程：打开首页 → 进入任一 Skill → 上传 `data/samples/` 里的示例 CSV → 确认能出报告。

---

## 交给面试官之前

- [ ] 在线地址能打开，且 `mock_mode` 为 `false`
- [ ] 完整跑通一次「生成 → 执行 → 看报告」
- [ ] 把地址填进根目录 `README.md` 和 `demo/README.md`，提交推送
- [ ] **发链接前先访问一次把服务唤醒**
- [ ] 确认 API 余额充足——面试官试用也会消耗额度

> 💡 即使额度耗尽或 Key 失效，系统会自动降级为**演示模式**：
> 数值部分仍由计算引擎真实算出，只有文字分析变成预置内容。
> 面试官不会看到报错页面，但完整能力还是要保证的。

---

## 安全提醒

- `.env` 已被 `.gitignore` 排除，推送前可再确认：
  ```bash
  cd "/Users/yuanchenbo/Desktop/数花科技_面试题" && git ls-files | grep -i "\.env$" || echo "✅ 无 .env 被跟踪"
  ```
- Key 只能通过平台的环境变量/Secret 注入，不要写进任何文件
- 当前 `main.py` 中 CORS 是全开的（为了兼容直接双击 HTML 打开）。
  这是 Demo 取舍，**生产环境必须收紧为具体域名白名单**
