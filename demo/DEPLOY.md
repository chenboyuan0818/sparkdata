# 部署指南

目标：给面试官一个**点开就能用**的公开链接。

已提供 `Dockerfile`（通用）、`Procfile` 与 `runtime.txt`（平台自动识别用）。

---

## 推荐：Hugging Face Spaces

**为什么推荐它**：真正免费、不需要绑卡；休眠后唤醒只要十几秒。
相比之下 Render 免费版冷启动要 50 秒以上——面试官点开链接干等近一分钟，观感很差。

### 步骤

**1. 注册并新建 Space**

访问 https://huggingface.co/new-space

| 字段 | 填什么 |
|---|---|
| Space name | `skill-platform`（或你喜欢的名字） |
| License | `mit` |
| **Space SDK** | **选 `Docker` → `Blank`** ← 关键，别选 Gradio |
| Hardware | `CPU basic · Free` |
| Visibility | `Public` |

**2. 配置 API Key（必须在推代码之前做）**

进入 Space → `Settings` → 找到 **Variables and secrets** → `New secret`

| Name | Value |
|---|---|
| `LLM_API_KEY` | 你的 `sk-ant-...` |

再加两个普通变量（`New variable`，不是 secret）：

| Name | Value |
|---|---|
| `LLM_PROVIDER` | `anthropic` |
| `LLM_MODEL` | `claude-opus-5` |

> ⚠️ **Key 必须走 Secret，不能写进代码。** Space 是公开仓库，任何人都能看到文件内容。

**3. 推送代码**

Space 页面上有 git 地址，形如 `https://huggingface.co/spaces/你的用户名/skill-platform`

```bash
cd "/Users/yuanchenbo/Desktop/数花科技_面试题/demo"

git init
git add .
git commit -m "企业岗位经验 Skill 生成平台"

git remote add space https://huggingface.co/spaces/<你的用户名>/skill-platform
git push space main
```

推送时会要求输入用户名和密码，**密码填 Access Token**（不是登录密码）：
https://huggingface.co/settings/tokens → New token → 权限选 `Write`

**4. 补一段 Space 配置**

HF 需要在 `README.md` 顶部读取一段 YAML 来识别 Space 配置。在 `demo/README.md` **最开头**加上：

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

> 这段在 GitHub 上会被渲染成一个小表格，略微影响观感。
> 介意的话就把 Space 作为独立仓库单独维护，GitHub 那份 README 保持干净。

**5. 等待构建**

Space 页面会显示 `Building`，约 2-4 分钟。变成 `Running` 后链接即可访问：

```
https://huggingface.co/spaces/<你的用户名>/skill-platform
```

---

## 备选：Render

免费、界面简单，但**冷启动慢**（休眠后首次访问要等 50 秒以上）。

1. https://render.com → `New` → `Web Service` → 连接 GitHub 仓库
2. 配置：

| 字段 | 值 |
|---|---|
| Root Directory | `demo` |
| Runtime | `Docker` |
| Instance Type | `Free` |

3. `Environment` 标签页加环境变量：`LLM_API_KEY`（标记为 secret）、`LLM_PROVIDER=anthropic`、`LLM_MODEL=claude-opus-5`

Render 会自动注入 `$PORT`，Dockerfile 已经处理好了。

---

## 部署后必做的检查

```bash
# 换成你的实际地址
BASE=https://你的用户名-skill-platform.hf.space

curl -s $BASE/api/status
```

期望输出里 `"mock_mode": false`、`"model": "claude-opus-5"`。

**如果 `mock_mode` 是 `true`**，说明 Key 没被读到：

- 确认 Secret 名字是 `LLM_API_KEY`（大小写敏感）
- 确认 `LLM_PROVIDER` 设成了 `anthropic`
- HF Spaces 改完 Secret 需要 `Settings → Factory rebuild` 才会生效

然后在浏览器里手动跑一遍：打开首页 → 进入任一 Skill → 上传 `data/samples/` 里的示例 CSV → 确认能出报告。

---

## 交给面试官之前

- [ ] 确认在线地址能打开，且 `mock_mode` 为 `false`
- [ ] 完整跑通一次「生成 → 执行 → 看报告」
- [ ] 把在线地址填进根目录 `README.md` 和 `demo/README.md`
- [ ] **访问一次链接把服务唤醒**（HF Space 长时间无访问会休眠）
- [ ] 确认 API 余额充足——面试官试用也会消耗额度

> 💡 即使额度耗尽或 Key 失效，系统会自动降级为**演示模式**：
> 数值部分仍由计算引擎真实算出，只有文字分析变成预置内容。
> 面试官不会看到报错页面，但完整能力还是要保证的。

---

## 安全提醒

- `.env` 已在 `.gitignore` 中，**但推送前务必确认一次**：
  ```bash
  git status --short | grep -i env
  ```
  应该没有任何输出（或只有 `.env.example`）
- Key 只能通过平台的 Secret 机制注入，不要写进任何文件
- 当前 `main.py` 中 CORS 是全开的（为了兼容直接双击 HTML 打开）。
  这是 Demo 取舍，**生产环境必须收紧为具体域名白名单**
