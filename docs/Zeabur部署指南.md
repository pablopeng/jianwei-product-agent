# Zeabur 部署指南

仓库：https://github.com/pablopeng/jianwei-product-agent （私有）

## 操作步骤

1. 在 Zeabur 创建项目，添加服务，选择 GitHub 仓库 `pablopeng/jianwei-product-agent` 的 `main` 分支。授权 Zeabur 读取该私有仓库；根目录保持 `/`。仓库内 Dockerfile 用于构建。
2. 在环境变量中填写下表。先绑定一个域名（例如自行选择的 `xxx.zeabur.app`），再把该完整主机名填入 `AGENT_ALLOWED_HOSTS`。未完成访问配置前，服务会拒绝启动。
3. 添加持久化卷，挂载路径 `/data`，保存会话、上传附件、配置与评测记录。使用单实例；当前文件存储和进程内异步任务不适用于多副本。不要把卷挂载到 `/app`，那会覆盖代码。
4. 暴露 HTTP 端口 `8080`；若平台设置其他 `PORT`，应用自动读取。绑定 HTTPS 域名后部署。
5. 打开域名。浏览器弹出访问验证，用户名填 `demo`，密码为自己设置的 `DEMO_ACCESS_PASSWORD`。所有拿到口令的人均为可信演示参与者，可使用共享模型配额并修改业务配置；请仅面向小范围体验，不当作完整的企业权限系统。
6. 选择样例或上传商品材料，提交生成要求，检查并确认一阶段档案，再生成卖点。评测中心可建立人工评审批次。重启服务后，用同一浏览器访问，检查历史会话仍在。

| 环境变量 | 填写方式 |
|---|---|
| `DEEPSEEK_API_KEY` | 你的有效密钥，只填在 Zeabur 环境变量，不提交 Git |
| `DEEPSEEK_MODEL` | 与本地验证相同的可用模型；默认 `deepseek-flash` |
| `DEEPSEEK_VISION_MODEL` | 必须可实际读取图片；默认同文本模型。换模型后先测一张商详图片 |
| `DEMO_ACCESS_PASSWORD` | 自行生成至少16位随机口令，私下发给体验者 |
| `AGENT_ALLOWED_HOSTS` | 绑定的域名，不含协议或斜杠；多个用英文逗号分隔，非标准端口需带端口 |
| `AGENT_STATE_DIR` | `/data`，Dockerfile已设；需配持久化卷 |
| `AGENT_DEPLOYMENT` | `cloud`，Dockerfile已设 |
| `HOST` / `PORT` | `0.0.0.0` / `8080`，Dockerfile已设；以服务暴露端口为准 |

本地 `.env.local`、浏览器身份、会话、CLI授权、密钥和登录态均不随仓库上传。云端模型密钥由环境变量管理，前端不会读到密钥值；修改本地模型文件不会覆盖环境变量。

## 飞书导出能力的部署边界

本轮14商品结果表及方案文档已经由本机已授权的 `lark-cli` 创建。标准 Docker 镜像只包含 Python 应用，**没有安装、也没有迁移本机飞书CLI授权**。因此云端的对话生成、档案确认、配置及本地评测记录可用，但新建飞书表格、回收飞书评分和导出飞书文档依赖另行配置飞书集成；未配置时会明确报未安装CLI，不会伪造成功。

近期可继续在本机评测中心管理飞书评审批次。正式团队服务应把该部分改为服务端飞书应用 OAuth/OpenAPI 接入与每用户凭证管理，而不是上传个人CLI登录文件。此项是云端集成待办，不在本次已完成能力中。

## 验证与限制

已完成Python HTTP层的云端口令、Host、Origin、Secure Cookie及缺配置拒绝启动测试。当前环境未提供Docker运行时，未实际构建/启动镜像；最终Zeabur部署由使用者执行并确认公网URL。本地批量生成耗时不是云端性能承诺。

服务重启会终止尚未结束的进程内任务；重新打开后可重试。单实例、小规模演示适用；正式使用需要任务队列、数据库、独立管理员权限和配额控制。

## 官方参考

- [Dockerfile部署](https://zeabur.com/docs/en-US/deploy/methods/dockerfile)
- [创建服务与GitHub导入](https://zeabur.com/docs/en-US/deploy/create/create-service)
- [持久化卷](https://zeabur.com/docs/en-US/operations/data/volumes)
- [环境变量与部署建议](https://zeabur.com/docs/en-US/get-started/best-practices)
