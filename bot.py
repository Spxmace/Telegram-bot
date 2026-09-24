# ============================================================
# AUTO-INSTALL DEPENDENCIES
# ============================================================

import subprocess
import sys
import importlib


def _install_if_missing(package_name, import_name=None):
    if import_name is None:
        import_name = package_name
    try:
        importlib.import_module(import_name)
        return True
    except ImportError:
        print(f"📦 Installing {package_name}...")
        try:
            subprocess.check_call([
                sys.executable, "-m", "pip", "install",
                "--upgrade", package_name
            ])
            print(f"✅ {package_name} installed.")
            return True
        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to install {package_name}: {e}")
            return False


_REQUIRED = [
    ("python-telegram-bot", "telegram"),
    ("requests", "requests"),
]

print("=" * 60)
print("   Checking dependencies...")
print("=" * 60)

for _pkg, _imp in _REQUIRED:
    _install_if_missing(_pkg, _imp)

print("=" * 60)
print()


# ============================================================
# NOW IMPORT EVERYTHING
# ============================================================

import asyncio
import logging
import re
import time
from typing import Optional

import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

RAILWAY_API = "https://backboard.railway.com/graphql/v2"
GITHUB_API = "https://api.github.com"

DEFAULT_PORT = 3000

DEPLOY_MAX_ATTEMPTS = 120
DEPLOY_POLL_SECONDS = 10
DOMAIN_MAX_RETRIES = 5
DOMAIN_RETRY_DELAY = 6

DASHBOARD_PATH = "/dashboard/"


# ============================================================
# CURATED REGIONS
# ============================================================

CURATED_REGIONS = {
    "us-west2": {
        "flag": "🇺🇸",
        "label": "US West (California)",
        "location": "US West",
    },
    "us-east4-eqdc4a": {
        "flag": "🇺🇸",
        "label": "US East (Virginia)",
        "location": "US East",
    },
    "europe-west4-drams3a": {
        "flag": "🇳🇱",
        "label": "EU West (Amsterdam)",
        "location": "EU West",
    },
    "asia-southeast1-eqsg3a": {
        "flag": "🇸🇬",
        "label": "Southeast Asia (Singapore)",
        "location": "Southeast Asia",
    },
}

CURATED_REGION_ORDER = [
    "us-west2",
    "us-east4-eqdc4a",
    "europe-west4-drams3a",
    "asia-southeast1-eqsg3a",
]


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# TELEGRAM BOT TOKEN
# ============================================================

import os

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError(
        "TELEGRAM_BOT_TOKEN environment variable is not set!"
)

# ============================================================
# USER SESSIONS
# ============================================================

sessions = {}


# ============================================================
# RAILWAY GRAPHQL REQUEST
# ============================================================

def railway_request(token: str, query: str, variables: Optional[dict] = None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"query": query, "variables": variables or {}}

    try:
        response = requests.post(RAILWAY_API, headers=headers, json=payload, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"Railway connection error: {e}")

    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(f"Railway returned invalid JSON. HTTP {response.status_code}")

    if response.status_code >= 400:
        raise RuntimeError(f"Railway HTTP {response.status_code}: {body}")

    if body.get("errors"):
        messages = []
        for error in body["errors"]:
            message = error.get("message", "Unknown GraphQL error")
            extensions = error.get("extensions", {})
            trace_id = extensions.get("traceId")
            if trace_id:
                message += f" | traceId={trace_id}"
            messages.append(message)
        raise RuntimeError(" | ".join(messages))

    if "data" not in body:
        raise RuntimeError(f"Railway response has no data: {body}")

    return body["data"]


# ============================================================
# GITHUB REPOSITORY PARSER
# ============================================================

def parse_github_repo(text: str) -> Optional[str]:
    text = text.strip()

    match = re.match(r"^https?://github\.com/([^/\s]+)/([^/\s]+?)/?$", text, re.IGNORECASE)
    if match:
        owner = match.group(1)
        repo = match.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return f"{owner}/{repo}"

    match = re.match(r"^([^/\s]+)/([^/\s]+)$", text)
    if match:
        owner = match.group(1)
        repo = match.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return f"{owner}/{repo}"

    return None


# ============================================================
# CHECK GITHUB PUBLIC REPOSITORY
# ============================================================

def check_github_repo(repo: str):
    url = f"{GITHUB_API}/repos/{repo}"
    try:
        response = requests.get(url, timeout=20, headers={"Accept": "application/vnd.github+json"})
    except requests.RequestException as e:
        raise RuntimeError(f"GitHub connection error: {e}")

    if response.status_code == 404:
        raise RuntimeError("Repository پیدا نشد یا Public نیست.")
    if response.status_code != 200:
        raise RuntimeError(f"GitHub HTTP {response.status_code}")

    try:
        return response.json()
    except ValueError:
        raise RuntimeError("GitHub response is invalid.")


# ============================================================
# GET RAILWAY WORKSPACES
# ============================================================

def get_workspaces(token: str):
    query = """
    query {
        me {
            workspaces {
                id
                name
            }
        }
    }
    """
    data = railway_request(token, query)
    me = data.get("me")
    if not me:
        raise RuntimeError("Railway account information was not returned.")
    return me.get("workspaces", []) or []


# ============================================================
# GET AVAILABLE REGIONS + FILTER
# ============================================================

def get_supported_regions(token: str):
    query = """
    query {
        regions {
            id
            name
            country
            location
        }
    }
    """
    data = railway_request(token, query)
    all_regions = data.get("regions") or []

    available_names = {(r.get("name") or r.get("id")) for r in all_regions}

    supported = []
    for name in CURATED_REGION_ORDER:
        if name in available_names and name in CURATED_REGIONS:
            meta = CURATED_REGIONS[name]
            supported.append({
                "name": name,
                "flag": meta["flag"],
                "label": meta["label"],
                "location": meta["location"],
            })

    if not supported:
        logger.warning("No curated regions matched. Falling back to raw list.")
        seen_locs = set()
        for r in all_regions:
            loc = r.get("location") or r.get("country") or r.get("name")
            if loc in seen_locs:
                continue
            seen_locs.add(loc)
            name = r.get("name") or r.get("id")
            supported.append({
                "name": name,
                "flag": "🌍",
                "label": loc,
                "location": loc,
            })

    return supported


# ============================================================
# CREATE PROJECT
# ============================================================

def create_project(token: str, workspace_id: str, name: str):
    query = """
    mutation ProjectCreate($input: ProjectCreateInput!) {
        projectCreate(input: $input) {
            id
            name
            environments {
                edges {
                    node {
                        id
                        name
                    }
                }
            }
        }
    }
    """
    variables = {"input": {"name": name, "workspaceId": workspace_id}}
    data = railway_request(token, query, variables)
    project = data.get("projectCreate")
    if not project:
        raise RuntimeError("Project was not created.")
    return project


# ============================================================
# GET PRODUCTION ENVIRONMENT
# ============================================================

def get_production_environment(project: dict):
    environments = project.get("environments", {}).get("edges", [])
    if not environments:
        raise RuntimeError("No Railway environment found.")
    for edge in environments:
        node = edge.get("node", {})
        if node.get("name", "").lower() == "production":
            return node
    return environments[0]["node"]


# ============================================================
# CREATE SERVICE — branch: main
# ============================================================

def create_service(token: str, project_id: str, service_name: str, repo: str):
    query = """
    mutation ServiceCreate($input: ServiceCreateInput!) {
        serviceCreate(input: $input) {
            id
            name
            projectId
        }
    }
    """
    try:
        data = railway_request(token, query, {
            "input": {
                "projectId": project_id,
                "name": service_name,
                "source": {"repo": repo, "branch": "main"},
            }
        })
        service = data.get("serviceCreate")
        if service:
            logger.info("Service created on branch=main")
            return service
    except Exception as exc:
        logger.warning("create_service with branch=main failed: %s", exc)

    data = railway_request(token, query, {
        "input": {
            "projectId": project_id,
            "name": service_name,
            "source": {"repo": repo},
        }
    })
    service = data.get("serviceCreate")
    if not service:
        raise RuntimeError("Service was not created.")
    return service


# ============================================================
# SET REGION
# ============================================================

def set_region(token: str, service_id: str, environment_id: str, region: str):
    query = """
    mutation ServiceInstanceUpdate(
        $serviceId: String!,
        $environmentId: String!,
        $input: ServiceInstanceUpdateInput!
    ) {
        serviceInstanceUpdate(
            serviceId: $serviceId,
            environmentId: $environmentId,
            input: $input
        )
    }
    """
    variables = {
        "serviceId": service_id,
        "environmentId": environment_id,
        "input": {
            "multiRegionConfig": {region: {"numReplicas": 1}},
            "restartPolicyType": "ON_FAILURE",
            "restartPolicyMaxRetries": 10,
        },
    }
    logger.info("Setting region to %s", region)
    return railway_request(token, query, variables)


# ============================================================
# VERIFY REGION
# ============================================================

def verify_region(token: str, service_id: str, environment_id: str):
    query = """
    query ServiceInstance($serviceId: String!, $environmentId: String!) {
        serviceInstance(serviceId: $serviceId, environmentId: $environmentId) {
            region
            multiRegionConfig
        }
    }
    """
    try:
        data = railway_request(token, query, {
            "serviceId": service_id,
            "environmentId": environment_id,
        })
        instance = data.get("serviceInstance") or {}
        region = instance.get("region")
        if region:
            return region
        mrc = instance.get("multiRegionConfig") or {}
        if isinstance(mrc, dict) and mrc:
            return list(mrc.keys())[0]
        return None
    except Exception as exc:
        logger.warning("verify_region failed: %s", exc)
        return None


# ============================================================
# SET ENV VARIABLES
# ============================================================

def set_service_variables(token: str, project_id: str, environment_id: str, service_id: str, variables_map: dict):
    query = """
    mutation VariableCollectionUpsert($input: VariableCollectionUpsertInput!) {
        variableCollectionUpsert(input: $input)
    }
    """
    variables = {
        "input": {
            "projectId": project_id,
            "environmentId": environment_id,
            "serviceId": service_id,
            "variables": variables_map,
        }
    }
    return railway_request(token, query, variables)


# ============================================================
# DEPLOY SERVICE
# ============================================================

def deploy_service(token: str, service_id: str, environment_id: str):
    query = """
    mutation ServiceInstanceDeployV2($serviceId: String!, $environmentId: String!) {
        serviceInstanceDeployV2(serviceId: $serviceId, environmentId: $environmentId)
    }
    """
    variables = {"serviceId": service_id, "environmentId": environment_id}
    data = railway_request(token, query, variables)
    deployment_id = data.get("serviceInstanceDeployV2")
    if not deployment_id:
        raise RuntimeError("Railway did not return a deployment ID.")
    return deployment_id


# ============================================================
# GET DEPLOYMENT
# ============================================================

def get_deployment(token: str, deployment_id: str):
    query = """
    query Deployment($id: String!) {
        deployment(id: $id) {
            id
            status
        }
    }
    """
    data = railway_request(token, query, {"id": deployment_id})
    return data.get("deployment")


# ============================================================
# WAIT FOR DEPLOYMENT
# ============================================================

def wait_for_deployment(token: str, deployment_id: str, update_callback=None):
    success_statuses = {"SUCCESS"}
    failure_statuses = {"FAILED", "CRASHED", "REMOVED", "CANCELED", "CANCELLED"}
    last_status = None

    for attempt in range(DEPLOY_MAX_ATTEMPTS):
        try:
            deployment = get_deployment(token, deployment_id)
        except Exception as exc:
            logger.warning("get_deployment attempt %d failed: %s", attempt, exc)
            time.sleep(DEPLOY_POLL_SECONDS)
            continue

        status = deployment.get("status", "UNKNOWN") if deployment else "UNKNOWN"

        if status != last_status:
            logger.info("Deployment status: %s", status)
            if update_callback:
                try:
                    update_callback(status)
                except Exception:
                    pass
            last_status = status

        if status in success_statuses:
            return True, status
        if status in failure_statuses:
            return False, status

        time.sleep(DEPLOY_POLL_SECONDS)

    return False, "TIMEOUT"


# ============================================================
# CREATE RAILWAY DOMAIN
# ============================================================

def create_domain(token: str, service_id: str, environment_id: str, port: int):
    query = """
    mutation ServiceDomainCreate($input: ServiceDomainCreateInput!) {
        serviceDomainCreate(input: $input) {
            id
            domain
            serviceId
            environmentId
            targetPort
        }
    }
    """
    variables = {
        "input": {
            "serviceId": service_id,
            "environmentId": environment_id,
            "targetPort": port,
        }
    }
    last_error = None

    for attempt in range(DOMAIN_MAX_RETRIES):
        try:
            data = railway_request(token, query, variables)
            domain = data.get("serviceDomainCreate")
            if domain:
                return domain
        except Exception as exc:
            last_error = exc
        time.sleep(DOMAIN_RETRY_DELAY)

    if last_error:
        raise last_error
    return None


# ============================================================
# BUILD DASHBOARD URL
# ============================================================

def build_dashboard_url(domain: str) -> str:
    if not domain:
        return ""
    if domain.startswith(("http://", "https://")):
        base = domain
    else:
        base = f"https://{domain}"
    base = base.rstrip("/")
    if base.endswith("/dashboard") or base.endswith("/dashboard/"):
        return base if base.endswith("/") else base + "/"
    return f"{base}{DASHBOARD_PATH}"


# ============================================================
# SAFE EDIT / SEND
# ============================================================

async def safe_edit(query, text: str, **kwargs):
    try:
        await query.edit_message_text(text, **kwargs)
    except Exception as exc:
        logger.warning("edit_message_text failed: %s", exc)


async def send_message(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
    except Exception:
        logger.exception("Could not send Telegram message")


# ============================================================
# START COMMAND
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sessions[user_id] = {"step": "repo"}
    await update.message.reply_text(
        "🚀 Railway GitHub Deployer\n\n"
        "لینک GitHub Repository را بفرست.\n\n"
        "مثال:\nhttps://github.com/user/repository\n\n"
        "یا:\nuser/repository\n\n"
        "⚠️ فقط Repository عمومی GitHub پشتیبانی می‌شود."
    )


# ============================================================
# RESET COMMAND
# ============================================================

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sessions.pop(user_id, None)
    await update.message.reply_text("♻️ نشست قبلی پاک شد.\n\nبرای شروع دوباره /start را بزن.")


# ============================================================
# HANDLE REPOSITORY
# ============================================================

async def handle_repo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    repo = parse_github_repo(text)

    if not repo:
        await update.message.reply_text(
            "❌ فرمت Repository صحیح نیست.\n\n"
            "مثال صحیح:\nhttps://github.com/user/repository\n\n"
            "یا:\nuser/repository"
        )
        return

    await update.message.reply_text("🔎 در حال بررسی Repository...")

    try:
        github_info = await asyncio.to_thread(check_github_repo, repo)
    except Exception as e:
        logger.exception("GitHub repository check failed")
        await update.message.reply_text(f"❌ Repository قابل دسترسی نیست.\n\n{str(e)[:2000]}")
        return

    repo_name = github_info.get("name", repo.split("/")[-1])
    sessions[user_id] = {"step": "port", "repo": repo, "repo_name": repo_name}

    await update.message.reply_text(
        f"✅ Repository پیدا شد:\n`{repo}`\n\n"
        f"🔌 حالا Port سرویس را بفرست.\n\nمثال:\n{DEFAULT_PORT}",
        parse_mode="Markdown",
    )


# ============================================================
# HANDLE TEXT
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = sessions.get(user_id)

    if not session:
        await handle_repo(update, context)
        return

    step = session.get("step")
    text = (update.message.text or "").strip()

    if step == "repo":
        await handle_repo(update, context)
        return

    if step == "port":
        if not text.isdigit():
            await update.message.reply_text("❌ پورت باید فقط عدد باشد.\n\nمثال:\n3000")
            return
        port = int(text)
        if port < 1 or port > 65535:
            await update.message.reply_text("❌ پورت باید بین 1 تا 65535 باشد.")
            return
        session["port"] = port
        session["step"] = "railway_token"
        await update.message.reply_text("🔑 حالا Railway Token را بفرست.")
        return

    if step == "railway_token":
        railway_token = text
        await update.message.reply_text("🔐 در حال بررسی Railway Token...")

        try:
            workspaces = await asyncio.to_thread(get_workspaces, railway_token)
        except Exception as e:
            logger.exception("Railway authentication error")
            await update.message.reply_text(f"❌ Railway Token معتبر نیست.\n\n{str(e)[:2500]}")
            return

        if not workspaces:
            await update.message.reply_text("❌ هیچ Workspace‌ای برای این Railway Token پیدا نشد.")
            return

        session["railway_token"] = railway_token
        session["workspaces"] = workspaces
        await update.message.reply_text("🌍 در حال دریافت Region‌های موجود...")

        try:
            regions = await asyncio.to_thread(get_supported_regions, railway_token)
        except Exception as e:
            logger.exception("regions query failed")
            await update.message.reply_text(f"❌ دریافت لیست Region‌ها ممکن نشد.\n\n{str(e)[:2500]}")
            return

        if not regions:
            await update.message.reply_text("❌ هیچ Region پشتیبانی‌شده‌ای پیدا نشد.")
            return

        session["regions"] = regions
        session["step"] = "region"

        rows = []
        for r in regions:
            flag = r.get("flag", "🌍")
            label = r.get("label") or r.get("name")
            name = r.get("name")
            rows.append([InlineKeyboardButton(f"{flag} {label}", callback_data=f"region:{name}")])

        await update.message.reply_text(
            "🌍 حالا Region موردنظر را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if step in ("region", "workspace", "deploying"):
        await update.message.reply_text("⏳ لطفاً از دکمه‌های بالا انتخاب کن.\nبرای شروع مجدد /reset را بزن.")
        return

    sessions.pop(user_id, None)
    await update.message.reply_text("⚠️ نشست قبلی نامعتبر شد.\n\nبرای شروع دوباره /start را بزن.")


# ============================================================
# HANDLE REGION
# ============================================================

async def handle_region(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    session = sessions.get(user_id)

    if not session:
        await safe_edit(query, "❌ نشست شما منقضی شده است.\n\nبرای شروع دوباره /start را بزن.")
        return

    region_name = query.data.split("region:", 1)[1]
    valid = {r.get("name") for r in session.get("regions", [])}
    if region_name not in valid:
        await safe_edit(query, "❌ Region نامعتبر است.")
        return

    picked = next((r for r in session.get("regions", []) if r.get("name") == region_name), None)
    flag = picked.get("flag", "🌍") if picked else "🌍"
    label = picked.get("label") if picked else region_name

    session["region"] = region_name
    session["region_flag"] = flag
    session["region_label"] = label
    session["step"] = "workspace"

    workspaces = session.get("workspaces", [])
    keyboard = [
        [InlineKeyboardButton(w.get("name", "Unnamed Workspace"), callback_data=f"workspace:{w.get('id')}")]
        for w in workspaces
    ]

    await safe_edit(
        query,
        f"🌍 Region انتخاب شد:\n{flag} {label}\n(`{region_name}`)\n\n📁 حالا Workspace موردنظر را انتخاب کن:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# HANDLE WORKSPACE
# ============================================================

async def handle_workspace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    session = sessions.get(user_id)

    if not session:
        await safe_edit(query, "❌ نشست شما منقضی شده است.\n\nبرای شروع دوباره /start را بزن.")
        return

    workspace_id = query.data.split("workspace:", 1)[1]
    workspace_name = "Unknown"

    for workspace in session.get("workspaces", []):
        if workspace.get("id") == workspace_id:
            workspace_name = workspace.get("name", "Unknown")
            break

    session["workspace_id"] = workspace_id
    session["workspace_name"] = workspace_name
    session["step"] = "deploying"

    flag = session.get("region_flag", "🌍")
    region_label = session.get("region_label", session.get("region"))

    await safe_edit(
        query,
        f"🚀 Deployment شروع شد...\n\n"
        f"📁 Workspace: {workspace_name}\n"
        f"🌍 Region: {flag} {region_label}\n"
        f"🔌 Port: {session.get('port')}\n\n"
        "⏳ لطفاً صبر کن..."
    )

    await deploy_process(user_id, context)


# ============================================================
# DEPLOY PROCESS
# ============================================================

async def deploy_process(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    session = sessions.get(user_id)
    if not session:
        return

    token = session.get("railway_token")
    repo = session.get("repo")
    region = session.get("region")
    port = session.get("port")
    workspace_id = session.get("workspace_id")
    workspace_name = session.get("workspace_name", "Unknown")
    region_flag = session.get("region_flag", "🌍")
    region_label = session.get("region_label", region)

    if not all([token, repo, region, port, workspace_id]):
        await send_message(context, user_id, "❌ اطلاعات Deployment ناقص است.\nبرای شروع دوباره /start را بزن.")
        sessions.pop(user_id, None)
        return

    project_id = None
    service_id = None
    deployment_id = None
    current_step = "init"

    try:
        repo_name = session.get("repo_name", repo.split("/")[-1])
        safe_repo_name = re.sub(r"[^a-zA-Z0-9-_]+", "-", repo_name).strip("-")
        if not safe_repo_name:
            safe_repo_name = "github-app"
        project_name = f"{safe_repo_name}-deployment"

        current_step = "create_project"
        await send_message(context, user_id, "📦 در حال ساخت Railway Project...")
        project = await asyncio.to_thread(create_project, token, workspace_id, project_name)
        project_id = project.get("id")
        if not project_id:
            raise RuntimeError("Project ID دریافت نشد.")

        current_step = "get_environment"
        environment = get_production_environment(project)
        environment_id = environment.get("id")
        if not environment_id:
            raise RuntimeError("Environment ID دریافت نشد.")

        current_step = "create_service"
        await send_message(context, user_id, "⚙️ در حال ساخت Service (branch: main)...")
        service = await asyncio.to_thread(create_service, token, project_id, safe_repo_name, repo)
        service_id = service.get("id")
        if not service_id:
            raise RuntimeError("Service ID دریافت نشد.")

        current_step = "set_region"
        await send_message(context, user_id, f"🌍 در حال تنظیم Region به {region_flag} {region_label}...")
        await asyncio.to_thread(set_region, token, service_id, environment_id, region)

        current_step = "verify_region"
        actual_region = await asyncio.to_thread(verify_region, token, service_id, environment_id)
        if actual_region and actual_region != region:
            await send_message(context, user_id, f"⚠️ هشدار: Region درخواستی `{region}` بود ولی Railway `{actual_region}` رو ست کرد.")
            logger.warning("Region mismatch: requested=%s actual=%s", region, actual_region)
        elif actual_region == region:
            logger.info("Region verified: %s", actual_region)

        current_step = "set_variables"
        await send_message(context, user_id, "🔌 در حال اعمال Port روی سرویس...")
        await asyncio.to_thread(set_service_variables, token, project_id, environment_id, service_id, {"PORT": str(port)})

        current_step = "deploy_service"
        await send_message(context, user_id, "🚀 در حال شروع Deployment...")
        deployment_id = await asyncio.to_thread(deploy_service, token, service_id, environment_id)

        current_step = "wait_for_deployment"
        await send_message(context, user_id, "⏳ Deployment در حال اجراست...\nممکن است چند دقیقه طول بکشد.")
        success, status = await asyncio.to_thread(wait_for_deployment, token, deployment_id, None)

        if not success:
            await send_message(
                context, user_id,
                f"❌ Deployment موفق نشد.\n\nStatus: {status}\n\n"
                f"Project ID: {project_id}\nService ID: {service_id}\n\n"
                "💡 از داشبورد Railway لاگ‌ها رو ببین.\nبرای شروع مجدد /start را بزن."
            )
            return

        current_step = "create_domain"
        await send_message(context, user_id, "🌐 Deployment موفق شد.\nدر حال ساخت Domain...")

        domain = None
        try:
            domain_data = await asyncio.to_thread(create_domain, token, service_id, environment_id, port)
            if domain_data:
                domain = domain_data.get("domain")
        except Exception as exc:
            logger.warning("Domain creation failed: %s", exc)

        message = (
            "✅ Deployment با موفقیت انجام شد!\n\n"
            f"📦 Repository:\n{repo}\n\n"
            f"🌿 Branch:\nmain\n\n"
            f"📁 Workspace:\n{workspace_name}\n\n"
            f"🌍 Region:\n{region_flag} {region_label} (`{actual_region or region}`)\n\n"
            f"🔌 Port:\n{port}\n\n"
            f"📦 Project ID:\n{project_id}\n\n"
            f"⚙️ Service ID:\n{service_id}\n\n"
            f"🚀 Deployment ID:\n{deployment_id}\n\n"
            f"📊 Status:\n{status}\n"
        )

        if domain:
            dashboard_url = build_dashboard_url(domain)
            message += f"\n🌐 Dashboard:\n{dashboard_url}\n"
        else:
            message += "\n⚠️ Domain به صورت خودکار ساخته نشد.\nاز داشبورد Railway بساز.\n"

        message += "\nبرای Deployment جدید دوباره /start را بزن."
        await send_message(context, user_id, message)

    except Exception as e:
        logger.exception("Deployment failed at step=%s", current_step)
        error_text = str(e)
        if len(error_text) > 3500:
            error_text = error_text[:3500] + "\n..."
        await send_message(
            context, user_id,
            f"❌ خطا در Deployment (مرحله: {current_step}):\n\n{error_text}\n\nبرای شروع دوباره /start را بزن."
        )

    finally:
        sessions.pop(user_id, None)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled exception:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ یک خطای غیرمنتظره رخ داد.\n\nلطفاً /start را بزن و دوباره تلاش کن.",
            )
    except Exception:
        logger.exception("Failed to send error message")


# ============================================================
# MAIN
# ============================================================

def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CallbackQueryHandler(handle_region, pattern=r"^region:"))
    application.add_handler(CallbackQueryHandler(handle_workspace, pattern=r"^workspace:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)

    print()
    print("=" * 60)
    print("Bot is running...")
    print("=" * 60)
    print()

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
