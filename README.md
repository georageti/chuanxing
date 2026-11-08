# 川星

每天自动收集 GitHub 上的高 star 项目，并按用途分类：Claude 和 Agent Skills、面试与刷题、学习路径和路线图、学习方法与效率、教程与课程、资源合集、AI 工具与应用、开发者工具、其他。

[![进入网站](https://img.shields.io/badge/%E8%BF%9B%E5%85%A5-%E7%BD%91%E7%AB%99-1f6b3a?style=for-the-badge)](https://georageti.github.io/chuanxing/)
[![每日日报](https://img.shields.io/badge/%E6%AF%8F%E6%97%A5-%E6%97%A5%E6%8A%A5-555555?style=for-the-badge)](daily/)

- 网页：<https://georageti.github.io/chuanxing/>（每天北京时间 8 点左右自动更新）
- 每日日报：[daily/](daily/)
- 自己 fork 一份并开启 GitHub Pages（main 分支的 `/docs` 文件夹）后，地址是 `https://<你的用户名>.github.io/<仓库名>/`

## 数据从哪来

1. **GitHub Trending**：抓取当天和本周的热门榜单。
2. **近期新星**：最近 30 天新建、star 已超过 300 的项目。
3. **各分类的高 star 项目**：用 `categories.json` 里每个分类的 `queries` 去搜，只要 star 超过 1000、没有归档的项目。

已经收录过的项目不会重复算作“新收录”。所有项目存在 `data/repos.json`。

## 中文翻译

项目简介会自动翻成中文，网页和日报默认显示中文。在网页上勾选“看英文原文”就切回英文；鼠标停在简介上，能看到另一种语言。

- 两个翻译引擎按顺序尝试：先用**谷歌翻译**的免费接口（通过 `curl` 调用），不行再用 **MyMemory** 的免费接口（每天约 5000 字符，只用来翻每天新增的项目，够用）。都失败就先保留英文，下次运行再补。不需要任何 API Key。
- 只翻译新出现、或者简介改过的项目，结果存在 `data/repos.json` 的 `desc_zh` 里。翻译失败就保留英文，下次运行再补。
- 机器翻译常把专有名词翻错，比如 LLM 翻成“法学硕士”、Obsidian 翻成“黑曜石”。`categories.json` 的 `translate.glossary` 是替换表，翻完自动纠正，可以自己往里加。
- 分类仍然按英文原文判断，翻译不影响分类。
- 只补翻译、不抓 GitHub：`python3 fetch.py --translate`。

## 怎么分类

规则写在 `categories.json` 里，不需要大模型。每个项目按下面的方式给每个分类打分：

| 命中 | 加分 |
|---|---|
| 项目的 GitHub 话题（topics）在分类的 `topics` 里 | 每个 +3 |
| 项目名包含分类的关键词 | 每个 +2 |
| 简介包含分类的关键词 | 每个 +1 |
| 项目是用这个分类的搜索语句搜到的 | +2 |

分类按顺序从上往下判断，第一个得分达到 3 分的就是主分类。顺序越靠前的分类越优先，比如一个带 `agent-skills` 话题的 AI 项目会归到“Claude 和 Agent Skills”，而不是“AI 工具与应用”。

想纠正某个项目的分类，在 `overrides` 里写一行：

```json
"overrides": { "owner/repo": "path" }
```

改完规则后，运行下面的命令。它不联网，只重新分类并重新生成日报和网页：

```bash
python3 fetch.py --reclassify
```

## 部署到 GitHub（每天自动运行）

1. 在 GitHub 上新建一个公开仓库，比如 `chuanxing`。
2. 把这个文件夹推送上去（见下方命令）。
3. 打开仓库的 **Settings → Pages**，Source 选 **Deploy from a branch**，分支选 `main`，目录选 `/docs`，保存。
4. 打开 **Actions** 页面，选中“每日抓取”，点 **Run workflow** 手动跑一次。之后每天北京时间 8 点左右自动运行。

推送命令（把地址换成你自己的仓库）：

```bash
cd ~/Desktop/Ideal/ChuanXing && git init -b main && git add . && git commit -m "川星初始化" && git remote add origin https://github.com/<你的用户名>/chuanxing.git && git push -u origin main
```

**国内访问提醒**：GitHub Pages 和 raw.githubusercontent.com 走的是同一家 CDN（Fastly），国内手机网络经常打不开。GitHub Actions 在云端运行，不受影响；只是看网页时，手机可能要开 VPN。想在国内稳定访问，可以把 `docs/` 文件夹放到 EdgeOne Pages 或 Cloudflare Pages，绑一个自己的子域名，比如 `xing.chuanstudio.cn`。

## 在本机运行

```bash
python3 fetch.py
```

不设置 `GITHUB_TOKEN` 也能运行，只是搜索接口每分钟只能调 10 次，跑一次大约 4 分钟。网页直接双击 `docs/index.html` 就能打开。
