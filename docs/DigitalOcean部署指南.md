# 点睛：DigitalOcean Droplet部署

项目为现有DigitalOcean项目 `dianjing-psq`。推荐最低试跑配置为Basic、Regular CPU、1GB内存、25GB磁盘，当前参考价6美元/月，最终以创建页确认价为准。512MB/4美元档尚未验证图片处理峰值内存，不能承诺稳定运行。只创建一台，不添加收费备份、数据库、独立卷或负载均衡。

代码已有Dockerfile。`deploy/digitalocean-bootstrap.sh`在Ubuntu服务器安装Docker、构建镜像并创建1GB swap；`deploy/digitalocean-start.sh`在配置密钥后启动。持久目录 `/opt/dianjing/state` 挂到容器 `/data`；重建应用容器不会移除该目录，删除整台Droplet仍会丢失数据，重要资料需另行备份。

运行密钥只写服务器上的 `/opt/dianjing/runtime.env`，权限600；不要提交Git，也不要填到公开脚本、cloud-init或日志。内容包括 `DEEPSEEK_API_KEY`、两个模型名称、`DEMO_ACCESS_PASSWORD`、`AGENT_ALLOWED_HOSTS`。参照已有环境变量说明。

应用只发布到服务器127.0.0.1:8080，由HTTPS反向代理向外提供访问；保持访问口令和域名校验。SSH使用单独部署密钥，私钥只保存在发起部署的设备，不提交仓库；新公钥加入账户/服务器前由用户确认。部署确认还需明确允许将现有DeepSeek密钥存到这台DigitalOcean服务器。

当前仓库的飞书CLI云端集成限制仍适用。创建Droplet不是部署成功：必须完成HTTPS、页面打开、模型调用、图片材料读取和会话持久化验证后，才交付公网地址。
