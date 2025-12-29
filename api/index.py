import os
import requests
from bs4 import BeautifulSoup
import boto3
from flask import Flask, jsonify, request
from zhipuai import ZhipuAI
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__)

def send_email(report_content):
    """发送邮件到QQ邮箱"""
    try:
        # 从环境变量获取邮箱配置
        smtp_server = os.environ.get("SMTP_SERVER", "smtp.qq.com")
        smtp_port = int(os.environ.get("SMTP_PORT", 587))
        sender_email = os.environ.get("SENDER_EMAIL")
        sender_password = os.environ.get("EMAIL_PASSWORD")  # 邮箱授权码
        
        if not sender_email or not sender_password:
            print("缺少邮箱配置信息")
            return False
        
        # 准备邮件内容
        msg = MIMEMultipart()
        msg['From'] = sender_email
        recipient_email = os.environ.get("RECIPIENT_EMAIL", sender_email)  # 默认发送给自己
        msg['To'] = recipient_email
        subject = os.environ.get("EMAIL_SUBJECT", "每日趣闻摘要 - AI生成")
        msg['Subject'] = subject
        
        msg.attach(MIMEText(report_content, 'plain', 'utf-8'))
        
        # 发送邮件
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        text = msg.as_string()
        server.sendmail(sender_email, recipient_email, text)
        server.quit()
        
        print("邮件发送成功")
        return True
    except Exception as e:
        print(f"邮件发送失败: {str(e)}")
        return False

def scrape_news():
    """从网页抓取新闻标题"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    # 抓取网页内容 - 从环境变量获取URL或使用默认值
    url = os.environ.get("NEWS_SOURCE_URL", "https://www.zhihu.com/hot")
    response = requests.get(url, headers=headers)
    response.encoding = 'utf-8'
    
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # 查找新闻标题 - 根据网页结构调整选择器
    news_items = soup.find_all('a', class_='HotItem-title')  # 根据实际网页结构调整
    
    if not news_items:
        # 如果没找到，尝试其他可能的类名
        news_items = soup.find_all('h2', class_='HotItem-title') or \
                     soup.find_all('div', class_='HotItem-content') or \
                     soup.find_all('a', href=lambda x: x and 'question' in x)
    
    titles = []
    max_news_count = int(os.environ.get("MAX_NEWS_COUNT", 10))  # 默认获取10条新闻
    for item in news_items[:max_news_count]:  # 只取指定数量的新闻
        if item.find('div'):
            title_elem = item.find(['span', 'div', 'p'])
        else:
            title_elem = item
        
        if title_elem:
            title = title_elem.get_text().strip()
            if title:
                titles.append(title)
    
    # 如果还是没抓到，使用备用方案
    if not titles:
        # 尝试使用通用选择器
        min_title_length = int(os.environ.get("MIN_TITLE_LENGTH", 10))  # 最小标题长度
        all_links = soup.find_all('a', href=True)
        for link in all_links:
            text = link.get_text().strip()
            if len(text) > min_title_length and '的' in text or '了' in text:  # 简单判断是否为标题
                titles.append(text)
                if len(titles) >= max_news_count:
                    break
    
    return titles

# 连通 4everland (S3 协议)
s3 = boto3.client("s3", 
    endpoint_url=os.environ.get("S3_ENDPOINT_URL", "https://endpoint.4everland.co"),
    aws_access_key_id=os.environ.get("S3_KEY"),
    aws_secret_access_key=os.environ.get("S3_SECRET")
)

@app.route('/api/cron/news')
def get_news():
    try:
        # 1. 抓取新闻
        news_list = scrape_news()
        if not news_list:
            # 如果网页抓取失败，回退到API方式，使用可配置的新闻数量
            backup_api_url = os.environ.get("NEWS_API_URL", "https://api.vvhan.com/api/hotlist/zhihu")
            res = requests.get(backup_api_url)
            max_news_count = int(os.environ.get("MAX_NEWS_COUNT", 10))
            news_list = [item['title'] for item in res.json()['data'][:max_news_count]]
        content = "\n".join(news_list)

        # 2. GLM 总结
        client = ZhipuAI(api_key=os.environ.get("GLM_KEY"))
        ai_prompt = os.environ.get("AI_PROMPT", "请把以下热搜总结成一段幽默的日常生活：")
        response = client.chat.completions.create(
            model="glm-4-flash",
            messages=[{"role": "user", "content": f"{ai_prompt}{content}"}]
        )
        final_report = response.choices[0].message.content

        # 3. 存入 4everland
        s3.put_object(
            Bucket=os.environ.get("BUCKET_NAME"),
            Key=os.environ.get("STORAGE_KEY", "daily_news.txt"),
            Body=final_report.encode('utf-8'),
            ContentType='text/plain; charset=utf-8'
        )
        
        # 4. 发送邮件
        send_email(final_report)
        
        return jsonify({"status": "success", "report": final_report})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

# 添加一个获取新闻报告的API端点
@app.route('/api/news')
def get_stored_news():
    try:
        response = s3.get_object(
            Bucket=os.environ.get("BUCKET_NAME"),
            Key=os.environ.get("STORAGE_KEY", "daily_news.txt")
        )
        content = response['Body'].read().decode('utf-8')
        return jsonify({"status": "success", "report": content})

    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

# 添加健康检查端点
@app.route('/api/health')
def health_check():
    return jsonify({"status": "healthy"})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))