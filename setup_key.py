from getpass import getpass
from agent.configuration import save_model

if __name__ == '__main__':
    print('密钥将保存在当前工程的.env.local中；输入时不会显示，也不会写入命令历史。')
    key = getpass('粘贴DeepSeek API Key并回车：').strip()
    save_model(key)
    print('已保存。打开页面的配置工作台，可测试连接；无需重启服务。')
