import asyncio
import os
import sys
import traceback
from typing import Optional

import aiohttp
from aiohttp import web
import cachetools
from gidgethub import aiohttp as gh_aiohttp
from gidgethub import routing
from gidgethub import sansio
from gidgethub import apps

from dotenv import load_dotenv

# 从.env文件加载环境变量
load_dotenv()

# 配置常量
CACHE_SIZE = 500
DEFAULT_PORT = 8080

router = routing.Router()
cache = cachetools.LRUCache(maxsize=CACHE_SIZE)
routes = web.RouteTableDef()

async def get_installation_token(gh: gh_aiohttp.GitHubAPI, installation_id: int) -> dict:
    """获取GitHub App的安装访问令牌"""
    return await apps.get_installation_access_token(
        gh,
        installation_id=installation_id,
        app_id=os.environ.get("GH_APP_ID"),
        private_key=os.environ.get("GH_PRIVATE_KEY")
    )

@routes.get("/", name="home")
async def handle_get(request: web.Request) -> web.Response:
    """处理首页GET请求"""
    return web.Response(text="Hello PyLadies Tunis")

@routes.post("/webhook")
async def webhook(request: web.Request) -> web.Response:
    """处理GitHub webhook请求"""
    try:
        body = await request.read()
        secret = os.environ.get("GH_SECRET")
        event = sansio.Event.from_http(request.headers, body, secret=secret)
        print(f"收到事件: {event.event}")
        
        if event.event == "ping":
            return web.Response(status=200)
            
        async with aiohttp.ClientSession() as session:
            gh = gh_aiohttp.GitHubAPI(session, "demo", cache=cache)
            await router.dispatch(event, gh)
            
            try:
                print("GH requests remaining:", gh.rate_limit.remaining)
            except AttributeError:
                pass
                
        return web.Response(status=200)
        
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        return web.Response(status=500)

@router.register("installation", action="created")
async def repo_installation_added(event, gh, *args, **kwargs):
    """处理GitHub App安装事件"""
    installation_id = event.data["installation"]["id"]
    installation_token = await get_installation_token(gh, installation_id)
    
    repo_name = event.data["repositories"][0]["full_name"]
    url = f"/repos/{repo_name}/issues"
    
    response = await gh.post(
        url,
        data={
            'title': '感谢安装本机器人',
            'body': '我会为您提供优质的服务!',
        },
        oauth_token=installation_token["token"]
    )
    print(response)

@router.register("pull_request", action="opened")
async def pr_opened(event, gh, *args, **kwargs):
    """处理新的Pull Request被打开的事件"""
    installation_id = event.data["installation"]["id"]
    installation_token = await get_installation_token(gh, installation_id)

    # 获取PR相关信息
    repo_full_name = event.data["repository"]["full_name"]
    pr_number = event.data["pull_request"]["number"]
    
    # 获取PR中的文件列表
    files_url = f"/repos/{repo_full_name}/pulls/{pr_number}/files"
    files = await gh.getitem(files_url, oauth_token=installation_token["token"])
    
    # 遍历文件列表,找出Python文件并获取内容
    python_files = []
    async with aiohttp.ClientSession() as session:
        for file in files:
            if file["filename"].endswith(".py"):
                async with session.get(file["raw_url"]) as response:
                    content = await response.text()
                    python_files.append({
                        "name": file["filename"],
                        "content": content
                    })
    
    if not python_files:
        return
        
    # 创建新分支
    branch_name = f"add-tests-{pr_number}"
    main_branch = await gh.getitem(
        f"/repos/{repo_full_name}/git/ref/heads/main",
        oauth_token=installation_token["token"]
    )
    await gh.post(
        f"/repos/{repo_full_name}/git/refs",
        data={
            "ref": f"refs/heads/{branch_name}",
            "sha": main_branch["object"]["sha"]
        },
        oauth_token=installation_token["token"]
    )
    
    # 为每个Python文件生成测试
    async with aiohttp.ClientSession() as session:
        for py_file in python_files:
            # 调用OpenAI API生成测试代码
            async with session.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY')}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "gpt-3.5-turbo",
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是一个Python测试专家,请根据提供的代码生成单元测试"
                        },
                        {
                            "role": "user", 
                            "content": f"请为以下Python代码生成单元测试:\n\n{py_file['content']}"
                        }
                    ],
                    "temperature": 0.7
                }
            ) as response:
                result = await response.json()
                test_content = result['choices'][0]['message']['content']
                
                # 如果返回的不是完整的测试代码,则使用模板包装
                if not test_content.startswith("import unittest"):
                    test_content = f"""import unittest
from {py_file['name'].replace('.py','')} import *

{test_content}
"""
                # 提交测试文件
                test_file_name = f"tests/test_{py_file['name']}"
                await gh.put(
                    f"/repos/{repo_full_name}/contents/{test_file_name}",
                    data={
                        "message": f"为 {py_file['name']} 添加单元测试",
                        "content": test_content,
                        "branch": branch_name
                    },
                    oauth_token=installation_token["token"]
                )
    
    # 创建PR
    await gh.post(
        f"/repos/{repo_full_name}/pulls",
        data={
            "title": "添加单元测试",
            "body": "自动生成的单元测试PR",
            "head": branch_name,
            "base": "main"
        },
        oauth_token=installation_token["token"]
    )
    
    # 创建评论
    comments_url = f"/repos/{repo_full_name}/issues/{pr_number}/comments"
    await gh.post(
        comments_url,
        data={
            "body": "已为Python文件生成单元测试并提交PR"
        },
        oauth_token=installation_token["token"]
    )

if __name__ == "__main__":  # pragma: no cover
    app = web.Application()
    app.router.add_routes(routes)
    
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    web.run_app(app, port=port)
