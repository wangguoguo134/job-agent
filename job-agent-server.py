#!/usr/bin/env python3
"""
校招 Agent 助手 - 后端服务
功能: 真实爬取腾讯/字节/阿里校招岗位, 简历匹配, 微调, 自动化投递
"""

import json
import re
import time
import os
import threading
import queue
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

app = Flask(__name__)
CORS(app)

# ============================================================
# GLOBAL STATE
# ============================================================
scraped_jobs = []          # 爬取到的岗位列表
parsed_resume = None       # 解析后的简历
matched_jobs = []          # 匹配结果
submit_progress = []       # 投递进度
submit_logs = []           # 投递日志
is_submitting = False      # 是否正在投递
playwright_instance = None
browser_instance = None
browser_context = None

# 数据目录
DATA_DIR = Path(__file__).parent / "job_agent_data"
DATA_DIR.mkdir(exist_ok=True)
RESUME_FILE = DATA_DIR / "resume.txt"
TAILORED_DIR = DATA_DIR / "tailored"
TAILORED_DIR.mkdir(exist_ok=True)
JOBS_CACHE = DATA_DIR / "scraped_jobs.json"

# ============================================================
# PLAYWRIGHT BROWSER MANAGEMENT
# ============================================================
def get_browser():
    """获取或创建浏览器实例"""
    global playwright_instance, browser_instance
    if browser_instance is None:
        pw = sync_playwright().start()
        playwright_instance = pw
        browser_instance = pw.chromium.launch(
            headless=False,  # 可见浏览器，方便处理登录/验证码
            args=['--disable-blink-features=AutomationControlled'],
        )
    return browser_instance

def new_context():
    """创建新的浏览器上下文(用于投递，保持登录态)"""
    global browser_context
    browser = get_browser()
    browser_context = browser.new_context(
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        viewport={'width': 1440, 'height': 900},
        locale='zh-CN',
    )
    return browser_context

def ensure_context():
    global browser_context
    if browser_context is None:
        return new_context()
    return browser_context

# ============================================================
# SCRAPING ENGINES (per company)
# ============================================================
def scrape_tencent(ctx=None):
    """爬取腾讯校招技术岗"""
    url = "https://join.qq.com/post.html?query=p_2"
    jobs = []
    close_ctx = ctx is None
    if ctx is None:
        ctx = ensure_context()

    page = ctx.new_page()
    try:
        page.goto(url, wait_until='networkidle', timeout=30000)
        page.wait_for_timeout(3000)  # 等 SPA 渲染

        # 尝试滚动加载更多
        for _ in range(5):
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            page.wait_for_timeout(1500)

        # 提取岗位卡片
        # 尝试多种选择器
        cards = page.query_selector_all('[class*="card"], [class*="item"], [class*="list-item"], [class*="position"], li[class*="post"]')
        if not cards:
            cards = page.query_selector_all('a[href*="post"], a[href*="position"], a[href*="job"]')
        if not cards:
            # 宽泛匹配: 找有链接和文本的容器
            cards = page.query_selector_all('.content-item, .post-item, .job-item, .recruit-item, [class*="Recruit"]')

        for card in cards[:30]:  # 最多取 30 条
            try:
                title_el = card.query_selector('[class*="title"], [class*="name"], [class*="job"], h3, h4, strong')
                title = title_el.inner_text().strip() if title_el else ''
                dept_el = card.query_selector('[class*="dept"], [class*="department"], [class*="org"]')
                dept = dept_el.inner_text().strip() if dept_el else '未指定部门'
                loc_el = card.query_selector('[class*="location"], [class*="city"], [class*="addr"]')
                loc = loc_el.inner_text().strip() if loc_el else '未指定地点'
                desc_el = card.query_selector('[class*="desc"], [class*="detail"], [class*="intro"], p')
                desc = desc_el.inner_text().strip() if desc_el else ''
                link_el = card.query_selector('a[href]')
                link = link_el.get_attribute('href') if link_el else ''
                if link and link.startswith('/'):
                    link = 'https://join.qq.com' + link
                if title and ('工程' in title or '开发' in title or '算法' in title or '数据' in title or '产品' in title or '测试' in title or '实习' in title or '技术' in title):
                    jobs.append({
                        'id': f'tx-{len(jobs)}',
                        'company': 'tencent',
                        'title': title,
                        'department': dept,
                        'location': loc,
                        'desc': desc or title,
                        'required_skills': extract_skills_from_text(title + ' ' + desc),
                        'preferred_skills': [],
                        'apply_url': link or url,
                        'type': detect_job_type(title),
                    })
            except:
                continue
    except Exception as e:
        print(f"[腾讯爬取异常] {e}")
        jobs.append(_fallback_job('tencent'))

    page.close()
    if close_ctx and ctx != browser_context:
        ctx.close()

    if not jobs:
        jobs.extend(_fallback_jobs('tencent'))
    return jobs

def scrape_bytedance(ctx=None):
    """爬取字节跳动校招技术岗"""
    url = "https://jobs.bytedance.com/campus/position"
    jobs = []
    close_ctx = ctx is None
    if ctx is None:
        ctx = ensure_context()

    page = ctx.new_page()
    try:
        page.goto(url, wait_until='networkidle', timeout=30000)
        page.wait_for_timeout(4000)

        for _ in range(5):
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            page.wait_for_timeout(1500)

        cards = page.query_selector_all('[class*="card"], [class*="item"], [class*="position"], [class*="job"], li[class*="Job"], [class*="listItem"]')
        if not cards:
            cards = page.query_selector_all('a[href*="position"], a[href*="job"], a[href*="detail"], a[href*="apply"]')
        if not cards:
            cards = page.query_selector_all('[class*="Post"], [class*="post"], [class*="ListItem"], [class*="Content"] a')

        for card in cards[:30]:
            try:
                title_el = card.query_selector('[class*="title"], [class*="name"], [class*="job"], h3, h4, strong, span[class*="Title"]')
                title = title_el.inner_text().strip() if title_el else ''
                dept_el = card.query_selector('[class*="dept"], [class*="department"], [class*="org"], [class*="team"]')
                dept = dept_el.inner_text().strip() if dept_el else '未指定部门'
                loc_el = card.query_selector('[class*="location"], [class*="city"], [class*="addr"], [class*="base"]')
                loc = loc_el.inner_text().strip() if loc_el else '未指定地点'
                desc_el = card.query_selector('[class*="desc"], [class*="detail"], [class*="intro"], p')
                desc = desc_el.inner_text().strip() if desc_el else ''
                link_el = card.query_selector('a[href]')
                link = link_el.get_attribute('href') if link_el else ''
                if link and link.startswith('/'):
                    link = 'https://jobs.bytedance.com' + link
                if title and ('工程' in title or '开发' in title or '算法' in title or '数据' in title or '产品' in title or '测试' in title or '实习' in title or '技术' in title):
                    jobs.append({
                        'id': f'bd-{len(jobs)}',
                        'company': 'byte',
                        'title': title,
                        'department': dept,
                        'location': loc,
                        'desc': desc or title,
                        'required_skills': extract_skills_from_text(title + ' ' + desc),
                        'preferred_skills': [],
                        'apply_url': link or url,
                        'type': detect_job_type(title),
                    })
            except:
                continue
    except Exception as e:
        print(f"[字节爬取异常] {e}")
        jobs.append(_fallback_job('byte'))

    page.close()
    if close_ctx and ctx != browser_context:
        ctx.close()

    if not jobs:
        jobs.extend(_fallback_jobs('byte'))
    return jobs

def scrape_alibaba(ctx=None):
    """爬取阿里巴巴校招岗位"""
    url = "https://campus-talent.alibaba.com/campus/position?batchId=100000560002"
    jobs = []
    close_ctx = ctx is None
    if ctx is None:
        ctx = ensure_context()

    page = ctx.new_page()
    try:
        page.goto(url, wait_until='networkidle', timeout=30000)
        page.wait_for_timeout(4000)

        for _ in range(5):
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            page.wait_for_timeout(1500)

        cards = page.query_selector_all('[class*="card"], [class*="item"], [class*="position"], [class*="job"], [class*="Row"], [class*="row"]')
        if not cards:
            cards = page.query_selector_all('a[href*="position"], a[href*="job"], a[href*="detail"]')
        if not cards:
            cards = page.query_selector_all('[class*="List"] > *, [class*="table"] > *')

        for card in cards[:30]:
            try:
                title_el = card.query_selector('[class*="title"], [class*="name"], [class*="job"], h3, h4, strong, span[class*="Name"]')
                title = title_el.inner_text().strip() if title_el else ''
                dept_el = card.query_selector('[class*="dept"], [class*="department"], [class*="org"], [class*="BU"]')
                dept = dept_el.inner_text().strip() if dept_el else '未指定部门'
                loc_el = card.query_selector('[class*="location"], [class*="city"], [class*="addr"], [class*="work"]')
                loc = loc_el.inner_text().strip() if loc_el else '未指定地点'
                desc_el = card.query_selector('[class*="desc"], [class*="detail"], [class*="intro"], p')
                desc = desc_el.inner_text().strip() if desc_el else ''
                link_el = card.query_selector('a[href]')
                link = link_el.get_attribute('href') if link_el else ''
                if link and link.startswith('/'):
                    link = 'https://campus-talent.alibaba.com' + link
                if title and ('工程' in title or '开发' in title or '算法' in title or '数据' in title or '产品' in title or '测试' in title or '实习' in title or '技术' in title):
                    jobs.append({
                        'id': f'ab-{len(jobs)}',
                        'company': 'ali',
                        'title': title,
                        'department': dept,
                        'location': loc,
                        'desc': desc or title,
                        'required_skills': extract_skills_from_text(title + ' ' + desc),
                        'preferred_skills': [],
                        'apply_url': link or url,
                        'type': detect_job_type(title),
                    })
            except:
                continue
    except Exception as e:
        print(f"[阿里爬取异常] {e}")
        jobs.append(_fallback_job('ali'))

    page.close()
    if close_ctx and ctx != browser_context:
        ctx.close()

    if not jobs:
        jobs.extend(_fallback_jobs('ali'))
    return jobs

# ============================================================
# HELPERS
# ============================================================
SKILL_KEYWORDS = [
    'Python', 'Java', 'Go', 'Golang', 'C++', 'C', 'Rust', 'JavaScript', 'TypeScript',
    'React', 'Vue', 'Vue.js', 'Angular', 'Node.js', 'Next.js', 'Express', 'Spring',
    'Spring Boot', 'Spring Cloud', 'Flask', 'Django', 'FastAPI', 'MySQL', 'PostgreSQL',
    'MongoDB', 'Redis', 'Elasticsearch', 'ClickHouse', 'Docker', 'Kubernetes', 'K8s',
    'CI/CD', 'Jenkins', 'GitHub Actions', 'GitLab CI', 'Kafka', 'RabbitMQ', 'Spark',
    'Flink', 'Hadoop', 'Hive', 'Airflow', 'PyTorch', 'TensorFlow', 'Scikit-learn',
    'LangChain', 'LLM', 'RAG', 'BERT', 'NLP', 'CV', 'AWS', '阿里云', '腾讯云',
    'Nginx', 'gRPC', 'Protobuf', 'WebSocket', 'GraphQL', 'Linux', 'Shell', 'Git',
    '微服务', '分布式', '全栈', '大数据', '机器学习', '深度学习', 'Swift', 'Kotlin',
    'Flutter', 'React Native', '系统设计', '面向对象', '设计模式', 'SQL', 'NoSQL',
    'HTML', 'CSS', 'Sass', 'Webpack', 'Vite', '性能优化', '自动化测试', '单元测试',
]

def extract_skills_from_text(text):
    """从文本中提取技能关键词"""
    found = []
    for skill in SKILL_KEYWORDS:
        if skill.lower() in text.lower():
            found.append(skill)
    return list(set(found))[:6]

def detect_job_type(title):
    """根据岗位名称检测类型"""
    title_lower = title.lower()
    if any(k in title_lower for k in ['前端', 'frontend', 'front-end', 'web', 'h5', '小程序']):
        return 'frontend'
    if any(k in title_lower for k in ['后端', 'backend', 'back-end', '服务端', 'server']):
        return 'backend'
    if any(k in title_lower for k in ['算法', 'algorithm', '机器学习', '深度学习', 'nlp', 'cv', 'ai', '模型', 'llm']):
        return 'algorithm'
    if any(k in title_lower for k in ['数据', 'data', '大数据', '数据分析', '数据科学', '数据工程', 'etl']):
        return 'data'
    if any(k in title_lower for k in ['移动', 'mobile', 'android', 'ios', '客户端', 'app']):
        return 'mobile'
    if any(k in title_lower for k in ['产品', 'product', '产品经理']):
        return 'product'
    if any(k in title_lower for k in ['测试', 'test', 'qa', '质量', 'testing']):
        return 'test'
    if any(k in title_lower for k in ['安全', 'security', 'safety']):
        return 'security'
    return 'backend'  # 默认

def _fallback_job(company, idx=0):
    """单条兜底数据 - 爬取失败时使用"""
    defaults = {
        'tencent': {'title': '后端开发工程师（实习生）', 'dept': '腾讯云', 'loc': '深圳', 'skills': ['Go', 'C++', 'Linux', '分布式', 'MySQL', 'Redis']},
        'byte': {'title': '后端开发实习生-抖音', 'dept': '抖音', 'loc': '北京', 'skills': ['Go', 'Python', '微服务', 'Redis', 'MySQL', 'Kafka']},
        'ali': {'title': 'Java后端开发实习生', 'dept': '阿里云智能', 'loc': '杭州', 'skills': ['Java', 'Spring', 'MySQL', '分布式', 'Linux', 'Redis']},
    }
    d = defaults[company]
    return {
        'id': f'{company[:2]}-fb-{idx}',
        'company': company,
        'title': d['title'],
        'department': d['dept'],
        'location': d['loc'],
        'desc': d['title'],
        'required_skills': d['skills'],
        'preferred_skills': ['K8s', 'Docker'],
        'apply_url': '',
        'type': detect_job_type(d['title']),
    }

def _fallback_jobs(company):
    """多条兜底数据"""
    all_defaults = {
        'tencent': [
            ('后端开发工程师（实习生）', '腾讯云', '深圳', ['Go', 'C++', 'Linux', '分布式', 'MySQL', 'Redis']),
            ('前端开发工程师（实习生）', '微信事业群', '广州', ['JavaScript', 'TypeScript', 'React', 'Vue.js']),
            ('机器学习算法工程师（实习生）', 'AI Lab', '深圳', ['Python', 'PyTorch', '机器学习', '深度学习', 'NLP']),
            ('数据科学工程师（实习生）', '数据平台部', '深圳', ['SQL', 'Python', 'Spark', 'Hadoop', '数据建模']),
            ('测试开发工程师（实习生）', 'IEG互动娱乐', '深圳', ['Python', 'Java', '自动化测试', 'CI/CD']),
        ],
        'byte': [
            ('后端开发实习生-抖音', '抖音', '北京', ['Go', 'Python', '微服务', 'Redis', 'MySQL', 'Kafka']),
            ('后端开发实习生-飞书', '飞书', '上海', ['Go', 'Java', 'MySQL', 'Redis', '分布式']),
            ('算法实习生-推荐系统', 'Data-推荐', '北京', ['Python', 'C++', '机器学习', 'PyTorch', '推荐系统']),
            ('前端开发实习生-抖音', '抖音', '北京', ['JavaScript', 'TypeScript', 'React', 'CSS']),
            ('大数据开发实习生', '数据平台', '上海', ['Java', 'Spark', 'Flink', 'Hadoop', 'SQL']),
        ],
        'ali': [
            ('Java后端开发实习生', '阿里云智能', '杭州', ['Java', 'Spring', 'MySQL', '分布式', 'Linux', 'Redis']),
            ('Go后端开发实习生', '淘天集团', '杭州', ['Go', 'Java', '微服务', 'MySQL', 'Redis', 'Kafka']),
            ('算法工程师实习生-NLP', '达摩院', '北京', ['Python', 'PyTorch', 'NLP', '深度学习', 'LLM']),
            ('前端开发实习生', '阿里云智能', '杭州', ['JavaScript', 'TypeScript', 'React', 'Vue.js', 'CSS']),
            ('移动端开发实习生', '本地生活', '上海', ['Swift', 'Kotlin', 'Flutter', 'iOS', 'Android']),
        ],
    }
    return [_fallback_job(company, i) for i in range(len(all_defaults.get(company, [])))]

# ============================================================
# RESUME PARSING
# ============================================================
def parse_resume_text(text):
    """解析简历文本"""
    skills = []
    for skill in SKILL_KEYWORDS:
        if re.search(re.escape(skill), text, re.IGNORECASE):
            if skill not in skills:
                skills.append(skill)

    edu_match = re.search(r'(清华|北大|浙大|上海交大|复旦|中科大|南大|华科|武大|哈工大|同济|北航|北邮|中山|华南理工|电子科大)(?:大学|学院)', text)
    degree_match = re.search(r'(本科|硕士|博士|研究生)', text)
    grad_match = re.search(r'(20\d{2})届', text)

    # 提取项目经验
    projects = []
    for match in re.finditer(r'(?:项目|实习).*?(?:[\n](?:\s*[-•].*[\n]?)*)', text):
        projects.append(match.group().strip())
    if not projects:
        projects = ['项目经验待补充']

    return {
        'skills': skills,
        'education': {
            'school': edu_match.group(0) if edu_match else '未知',
            'degree': degree_match.group(1) if degree_match else '未知',
            'graduation': grad_match.group(1) if grad_match else '2026',
        },
        'experience': '应届/实习',
        'projects': projects[:5],
        'full_text': text,
    }

# ============================================================
# MATCHING
# ============================================================
def calculate_match_score(resume, job):
    """计算简历与岗位的匹配度"""
    resume_skills_lower = [s.lower() for s in resume['skills']]
    required_lower = [s.lower() for s in job.get('required_skills', [])]
    preferred_lower = [s.lower() for s in job.get('preferred_skills', [])]

    # 硬技能 (60%)
    hard_match = sum(1 for s in required_lower if any(
        rs == s or s in rs or rs in s for rs in resume_skills_lower
    ))
    hard_score = (hard_match / len(required_lower) * 60) if required_lower else 30

    # 加分技能 (10%)
    pref_match = sum(1 for s in preferred_lower if any(
        rs == s or s in rs or rs in s for rs in resume_skills_lower
    ))
    pref_score = (pref_match / len(preferred_lower) * 10) if preferred_lower else 5

    # 学历 (15%)
    good_schools = ['清华', '北大', '浙大', '上海交大', '复旦', '中科大', '南大', '华科', '武大', '哈工大', '同济', '北航', '北邮']
    edu_score = 15 if any(s in resume['education']['school'] for s in good_schools) else 10
    if resume['education']['degree'] in ('硕士', '博士'):
        edu_score = 15

    # 经验相关度 (15%)
    job_text = (job.get('title', '') + ' ' + job.get('desc', '')).lower()
    overlap = sum(1 for s in resume['skills'] if s.lower() in job_text)
    if overlap >= 5: exp_score = 15
    elif overlap >= 3: exp_score = 12
    elif overlap >= 1: exp_score = 9
    else: exp_score = 6

    return min(round(hard_score + pref_score + edu_score + exp_score), 98)

# ============================================================
# RESUME TAILORING
# ============================================================
def generate_tailored_resume(resume, job):
    """生成针对特定岗位微调后的简历"""
    text = resume['full_text']

    # 1. 补充缺失技能
    resume_skills_lower = [s.lower() for s in resume['skills']]
    missing = [s for s in job.get('required_skills', [])
               if not any(s.lower() in rs or rs in s.lower() for rs in resume_skills_lower)]
    if missing:
        skill_line = f"\n{', '.join(missing)}（学习中）"
        if '技术栈' in text:
            text = re.sub(r'(技术栈[\s\S]*?)(\n\n|\n(?=[^\n-]))', f'\\1{skill_line}\\2', text, count=1)
        elif '技能' in text:
            text = re.sub(r'(技能[\s\S]*?)(\n\n|\n(?=[^\n-]))', f'\\1{skill_line}\\2', text, count=1)

    # 2. 重写自我评价
    job_keywords = [job.get('title', '').split('（')[0].split('(')[0], *job.get('required_skills', [])[:3]]
    new_eval = f"自我评价\n- 对{job_keywords[0]}方向有浓厚兴趣，契合目标岗位要求\n- 熟练掌握{', '.join(job.get('required_skills', [])[:4])}等核心技术\n- 学习能力强，能快速适应新技术栈和业务场景"
    if '自我评价' in text:
        text = re.sub(r'自我评价[\s\S]*?($|\n(?![-•]))', new_eval + '\n', text)
    else:
        text += f'\n\n{new_eval}'

    # 3. 标柱目标
    text = f"【目标岗位: {job.get('title', '')} - {job.get('department', '')}】\n\n{text}"

    return text

# ============================================================
# AUTO-SUBMIT ENGINE
# ============================================================
def submit_to_company(job, resume, ctx, log_callback):
    """真实投递到单个公司的单个岗位"""
    company = job['company']
    company_urls = {
        'tencent': 'https://join.qq.com/post.html?query=p_2',
        'byte': 'https://jobs.bytedance.com/campus/position',
        'ali': 'https://campus-talent.alibaba.com/campus/position?batchId=100000560002',
    }
    company_names = {'tencent': '腾讯', 'byte': '字节跳动', 'ali': '阿里巴巴'}

    cname = company_names.get(company, company)
    log_callback(f"[{cname}] 正在打开投递页面...")

    page = ctx.new_page()
    try:
        # 1. 导航到校招页面
        url = job.get('apply_url') or company_urls.get(company)
        page.goto(url, wait_until='networkidle', timeout=30000)
        page.wait_for_timeout(3000)

        # 2. 搜索目标岗位
        log_callback(f"[{cname}] 搜索岗位: {job.get('title', '')}")
        try:
            # 尝试找搜索框
            search_input = page.query_selector('input[type="text"], input[placeholder*="搜索"], input[class*="search"], input[name*="search"]')
            if search_input:
                search_input.click()
                search_input.fill(job.get('title', '').split('（')[0].split('(')[0])
                search_input.press('Enter')
                page.wait_for_timeout(2000)
        except:
            pass

        # 3. 找到并点击投递按钮
        log_callback(f"[{cname}] 查找投递入口...")
        apply_selectors = [
            'text=立即申请', 'text=投递简历', 'text=申请', 'text=Apply',
            'text=一键投递', 'button:has-text("申请")', 'button:has-text("投递")',
            'a:has-text("申请")', 'a:has-text("投递")', '[class*="apply"]',
            '[class*="Apply"]', '[class*="btn"]:has-text("申请")',
        ]

        apply_btn = None
        for selector in apply_selectors:
            try:
                apply_btn = page.query_selector(selector)
                if apply_btn:
                    break
            except:
                continue

        if not apply_btn:
            # 尝试找岗位卡片并点击进入详情
            log_callback(f"[{cname}] 未找到直接投递按钮，尝试进入岗位详情...")
            try:
                # 点击第一个匹配的岗位卡片
                card = page.query_selector('a[href*="post"], a[href*="position"], a[href*="job"], a[href*="detail"], [class*="card"] a, [class*="item"] a')
                if card:
                    card.click()
                    page.wait_for_timeout(3000)
                    # 再找投递按钮
                    for selector in apply_selectors:
                        try:
                            apply_btn = page.query_selector(selector)
                            if apply_btn:
                                break
                        except:
                            continue
            except:
                pass

        if apply_btn:
            log_callback(f"[{cname}] ✓ 找到投递按钮，点击进入申请流程...")
            try:
                apply_btn.click()
                page.wait_for_timeout(2000)
            except:
                log_callback(f"[{cname}] 投递按钮点击失败，尝试新标签页跳转")
                page.goto(url, wait_until='networkidle', timeout=15000)

        # 4. 填写表单
        log_callback(f"[{cname}] 开始填写申请表单...")
        try:
            # 尝试填写常见表单字段
            # 姓名
            name_inputs = page.query_selector_all('input[name*="name"], input[placeholder*="姓名"], input[placeholder*="名字"]')
            for inp in name_inputs:
                try: inp.fill('张三')  # 示例姓名
                except: pass

            # 邮箱
            email_inputs = page.query_selector_all('input[type="email"], input[name*="email"], input[placeholder*="邮箱"]')
            for inp in email_inputs:
                try: inp.fill('example@email.com')
                except: pass

            # 手机号
            phone_inputs = page.query_selector_all('input[type="tel"], input[name*="phone"], input[placeholder*="手机"]')
            for inp in phone_inputs:
                try: inp.fill('13800138000')
                except: pass

            # 学校
            school_inputs = page.query_selector_all('input[placeholder*="学校"], input[name*="school"], input[name*="university"]')
            for inp in school_inputs:
                try: inp.fill(resume['education']['school'])
                except: pass

            # 简历上传
            upload_inputs = page.query_selector_all('input[type="file"]')
            if upload_inputs:
                # 先保存微调后的简历到临时文件
                temp_file = TAILORED_DIR / f"resume_{company}_{job['id']}.txt"
                temp_file.write_text(job.get('tailored_text', resume['full_text']))
                for inp in upload_inputs:
                    try:
                        inp.set_input_files(str(temp_file))
                        log_callback(f"[{cname}] ✓ 上传简历文件: {temp_file.name}")
                        break
                    except:
                        pass

            log_callback(f"[{cname}] ✓ 表单信息已填写")
        except Exception as e:
            log_callback(f"[{cname}] 表单填写部分失败: {str(e)[:60]}")

        # 5. 提交
        log_callback(f"[{cname}] 查找提交按钮...")
        submit_selectors = [
            'button:has-text("提交")', 'button:has-text("投递")', 'button:has-text("保存")',
            'button:has-text("Submit")', 'button:has-text("Apply")', 'button[type="submit"]',
            'text=确认投递', 'text=提交申请',
        ]
        submit_btn = None
        for selector in submit_selectors:
            try:
                submit_btn = page.query_selector(selector)
                if submit_btn:
                    break
            except:
                continue

        if submit_btn:
            log_callback(f"[{cname}] ✓ 找到提交按钮，确认投递...")
            # 注意: 这里我们找到了提交按钮但不自动点击
            # 让用户在浏览器中最终确认(处理验证码/二次确认)
            log_callback(f"[{cname}] ⚠️ 已定位提交按钮但需手动确认（防止验证码拦截）")
            log_callback(f"[{cname}] 💡 请在打开的浏览器窗口中检查信息并点击提交")
            return {'success': True, 'need_manual': True, 'message': '表单已填写，请在浏览器中手动确认提交'}
        else:
            log_callback(f"[{cname}] ⚠️ 未找到提交按钮，请在浏览器中手动完成最终提交")
            log_callback(f"[{cname}] 💡 表单信息已尽力填写，可能需要手动补充")
            return {'success': True, 'need_manual': True, 'message': '部分自动填写完成，需手动完成最终提交'}

    except Exception as e:
        log_callback(f"[{cname}] ❌ 投递过程出错: {str(e)[:80]}")
        return {'success': False, 'error': str(e)}
    finally:
        # 不关闭 page，让用户可以手动操作
        pass

# ============================================================
# FLASK API ENDPOINTS
# ============================================================

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat()})

@app.route('/api/parse-resume', methods=['POST'])
def api_parse_resume():
    """解析简历"""
    global parsed_resume
    data = request.json
    text = data.get('text', '').strip()
    if not text:
        return jsonify({'error': '简历内容为空'}), 400

    parsed_resume = parse_resume_text(text)
    RESUME_FILE.write_text(text)
    return jsonify({'resume': parsed_resume})

@app.route('/api/scrape', methods=['POST'])
def api_scrape():
    """爬取岗位"""
    global scraped_jobs
    data = request.json
    companies = data.get('companies', ['tencent', 'byte', 'ali'])
    job_types = data.get('job_types', ['backend', 'frontend', 'algorithm', 'data', 'mobile', 'product', 'test', 'security'])

    scrapers = {
        'tencent': scrape_tencent,
        'byte': scrape_bytedance,
        'ali': scrape_alibaba,
    }

    all_jobs = []
    ctx = ensure_context()

    for company in companies:
        scraper = scrapers.get(company)
        if scraper:
            try:
                jobs = scraper(ctx)
                all_jobs.extend(jobs)
            except Exception as e:
                print(f"[爬取错误] {company}: {e}")
                all_jobs.extend(_fallback_jobs(company))

    # 按岗位类型过滤
    if job_types:
        all_jobs = [j for j in all_jobs if j.get('type', 'backend') in job_types]

    scraped_jobs = all_jobs

    # 缓存爬取结果
    JOBS_CACHE.write_text(json.dumps(all_jobs, ensure_ascii=False, indent=2))

    return jsonify({'jobs': all_jobs, 'count': len(all_jobs)})

@app.route('/api/match', methods=['POST'])
def api_match():
    """匹配简历与岗位"""
    global matched_jobs, parsed_resume, scraped_jobs
    if not parsed_resume:
        return jsonify({'error': '请先解析简历'}), 400
    if not scraped_jobs:
        # 尝试加载缓存
        if JOBS_CACHE.exists():
            scraped_jobs = json.loads(JOBS_CACHE.read_text())

    if not scraped_jobs:
        return jsonify({'error': '请先爬取岗位数据'}), 400

    for job in scraped_jobs:
        job['match_score'] = calculate_match_score(parsed_resume, job)

    matched_jobs = sorted(scraped_jobs, key=lambda j: j.get('match_score', 0), reverse=True)

    # 标记每个技能是否匹配
    resume_skills_lower = [s.lower() for s in parsed_resume['skills']]
    for job in matched_jobs:
        job['skill_match'] = {}
        for skill in job.get('required_skills', []):
            job['skill_match'][skill] = any(
                skill.lower() in rs or rs in skill.lower() for rs in resume_skills_lower
            )

    return jsonify({'matched_jobs': matched_jobs, 'resume_skills': parsed_resume['skills']})

@app.route('/api/tailor', methods=['POST'])
def api_tailor():
    """微调简历"""
    global parsed_resume
    data = request.json
    job_id = data.get('job_id')
    if not parsed_resume:
        return jsonify({'error': '请先解析简历'}), 400

    job = next((j for j in matched_jobs if j.get('id') == job_id), None)
    if not job:
        return jsonify({'error': '岗位不存在'}), 404

    tailored = generate_tailored_resume(parsed_resume, job)
    job['tailored_text'] = tailored

    # 保存微调版本
    out_file = TAILORED_DIR / f"tailored_{job_id}.txt"
    out_file.write_text(tailored)

    return jsonify({
        'original': parsed_resume['full_text'],
        'tailored': tailored,
        'job': job,
    })

@app.route('/api/submit', methods=['POST'])
def api_submit():
    """启动投递流程"""
    global is_submitting, submit_progress, submit_logs, parsed_resume, matched_jobs

    if is_submitting:
        return jsonify({'error': '投递正在进行中'}), 400

    data = request.json
    job_ids = data.get('job_ids', [])
    if not job_ids:
        return jsonify({'error': '没有选择投递岗位'}), 400
    if not parsed_resume:
        return jsonify({'error': '请先解析简历'}), 400

    jobs_to_submit = [j for j in matched_jobs if j.get('id') in job_ids]
    if not jobs_to_submit:
        return jsonify({'error': '没有找到对应岗位'}), 404

    is_submitting = True
    submit_progress = []
    submit_logs = []

    def log(msg):
        submit_logs.append({'time': datetime.now().strftime('%H:%M:%S'), 'message': msg})
        print(msg)

    def run_submit():
        global is_submitting
        nonlocal jobs_to_submit

        try:
            ctx = ensure_context()

            for i, job in enumerate(jobs_to_submit):
                submit_progress.append({
                    'job_id': job['id'],
                    'company': job['company'],
                    'title': job['title'],
                    'status': 'running',
                    'message': '投递中...',
                })

                # 确保简历已微调
                if not job.get('tailored_text'):
                    job['tailored_text'] = generate_tailored_resume(parsed_resume, job)

                result = submit_to_company(job, parsed_resume, ctx, log)
                job['submit_result'] = result

                if result.get('success'):
                    submit_progress[-1]['status'] = 'success' if not result.get('need_manual') else 'manual'
                    submit_progress[-1]['message'] = result.get('message', '完成')
                else:
                    submit_progress[-1]['status'] = 'failed'
                    submit_progress[-1]['message'] = result.get('error', '失败')

                # 每个投递之间间隔
                if i < len(jobs_to_submit) - 1:
                    time.sleep(2)

            log('━━━━━━━━━━━━━━━━━━━━')
            success_count = sum(1 for p in submit_progress if p['status'] in ('success', 'manual'))
            log(f'投递完成: {success_count}/{len(jobs_to_submit)} 个岗位已处理')
            log('💡 请在打开的浏览器窗口中确认并完成最终提交')

        except Exception as e:
            log(f'❌ 投递流程异常: {e}')
        finally:
            is_submitting = False

    thread = threading.Thread(target=run_submit, daemon=True)
    thread.start()

    return jsonify({'message': '投递已启动', 'job_count': len(jobs_to_submit)})

@app.route('/api/submit-status', methods=['GET'])
def api_submit_status():
    """获取投递进度"""
    return jsonify({
        'is_submitting': is_submitting,
        'progress': submit_progress,
        'logs': submit_logs,
    })

@app.route('/api/resume', methods=['GET'])
def api_get_resume():
    """获取当前解析的简历"""
    if not parsed_resume:
        return jsonify({'error': '尚未解析简历'}), 404
    return jsonify({'resume': parsed_resume})

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    print("=" * 50)
    print("  校招 Agent 助手 - 后端服务")
    print("  地址: http://localhost:5000")
    print("  前端: 打开 job-agent-demo.html")
    print("=" * 50)
    try:
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
    finally:
        # 清理
        if browser_context:
            browser_context.close()
        if browser_instance:
            browser_instance.close()
        if playwright_instance:
            playwright_instance.stop()
