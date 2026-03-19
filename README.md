# XQPS 绩效管理系统

基于 Streamlit + 飞书多维表的绩效管理系统，支持员工自评、上级评分、综合调整、历史档案等功能。

## 功能概览

- **员工自评**：工作目标、通用能力、领导力（管理者）自评与提交
- **上级评分**：管理者对下属进行评分与考核评语
- **综合调整**：一级部门负责人、分管高管对考核结果进行校准
- **历史信息**：查看上一次绩效结果与评语
- **演示入口**：支持三部门合并或单部门测试账号登录
- **扫码登录**：飞书 App 扫码授权（需 qrcode、pillow）
- **制度学习**：侧边栏链接至绩效管理制度文档

## 技术栈

- Python 3.10+
- Streamlit
- 飞书开放平台（OAuth + Bitable API）
- Pandas、Altair

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

复制 `.env.example` 为 `.env`，或配置 `.streamlit/secrets.toml`：

| 配置项 | 说明 |
|--------|------|
| FEISHU_APP_ID | 飞书应用 ID |
| FEISHU_APP_SECRET | 飞书应用密钥 |
| FEISHU_APP_TOKEN | 飞书多维表 App Token |
| FEISHU_TABLE_ID | 飞书多维表 Table ID |
| REDIRECT_URI | OAuth 回调地址（生产环境需配置公网域名） |
| APP_ENV | 运行环境：`production` / `staging` |
| ENABLE_DEMO_LOGIN | 是否开启演示登录：`true` / `false` |

### 3. 运行

```bash
streamlit run new_app.py --server.port 8501
```

### 4. 演示模式（测试环境）

```bash
APP_ENV=staging ENABLE_DEMO_LOGIN=true streamlit run new_app.py --server.port 8501
```

或使用启动脚本：

```bash
./run_demo.sh
```

**演示入口 URL：**

- **合并入口（三部门）**：http://localhost:8501/?demo_entry=1 或 ?demo_entry=1&demo_dept=all
- 人力资源部：http://localhost:8501/?demo_entry=1&demo_dept=hr
- 财富顾问部：http://localhost:8501/?demo_entry=1&demo_dept=wealth
- 研发质量保障部：http://localhost:8501/?demo_entry=1&demo_dept=rd

### 5. 演示账号配置

默认合并三部门：`demo_users.json`、`demo_users_hr.json`、`demo_users_wealth.json` 按顺序读取并去重。

- 研发质量部：`demo_users.json`（参考 `demo_users.example.json`）
- 人力资源部：`demo_users_hr.json`（参考 `demo_users_hr.example.json`）
- 财富顾问部：`demo_users_wealth.json`（参考 `demo_users_wealth.example.json`）

从飞书拉取真实员工 open_id：

```bash
python3 get_open_ids.py 人力资源部 > demo_users_hr.json
python3 get_open_ids.py 财富顾问部 > demo_users_wealth.json
python3 get_open_ids.py 研发质量保障部  # 输出到 demo_users.json
python3 get_open_ids.py -l   # 列出所有一级部门
```

## 项目结构

```
XQPS/
├── new_app.py              # 主应用
├── demo_entry.py           # 研发质量部演示入口
├── demo_entry_hr.py        # 人力资源部演示入口
├── demo_entry_wealth.py    # 财富顾问部演示入口
├── get_open_ids.py        # 从飞书拉取员工 open_id 工具
├── run_demo.sh            # 演示环境启动脚本
├── requirements.txt
├── .streamlit/
│   └── secrets.toml       # 密钥配置（勿提交）
├── demo_users.json         # 研发质量部测试账号（勿提交）
├── demo_users_hr.json      # 人力资源部测试账号（勿提交）
└── demo_users_wealth.json  # 财富顾问部测试账号（勿提交）
```

## 部署说明

生产环境需配置：

- `APP_ENV=production`
- `ENABLE_DEMO_LOGIN=false`
- `REDIRECT_URI` 为实际公网域名

## License

内部使用
