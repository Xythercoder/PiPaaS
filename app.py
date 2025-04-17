from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
import shutil
import requests
import subprocess
import yaml
import os
from dotenv import load_dotenv
import docker


app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')
client = docker.from_env()

load_dotenv()

CF_ZONE_ID = os.getenv('CF_ZONE_ID')
CF_API_TOKEN = os.getenv('CF_API_TOKEN')
CF_TUNNEL_ID = os.getenv('CF_TUNNEL_ID')
CF_ACCOUNT_ID = os.getenv('CF_ACCOUNT_ID')
CF_ACCOUNT_ID = os.getenv('CF_ACCOUNT_ID')
CF_DOMAIN = os.getenv('CF_DOMAIN')
CF_IP = os.getenv('CF_IP')

HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json"
}

TUNNEL_HOST = f"{CF_TUNNEL_ID}.cfargotunnel.com"


PROJECTS_DIR = os.path.join(os.getcwd(), 'user_projects')
os.makedirs(PROJECTS_DIR, exist_ok=True)


def bytes_to_mb(bytes_value):
    """Converts bytes to megabytes."""
    return f"{bytes_value / (1024 * 1024):.2f} MB" if bytes_value is not None else "N/A"


def get_blkio_value(blkio_list, op_type):
    return sum(item.get("value", 0) for item in blkio_list if item.get("op") == op_type)


def get_container_stats(container):
    raw_stats = container.stats(stream=False)

    # CPU Usage %
    cpu_delta = raw_stats["cpu_stats"]["cpu_usage"]["total_usage"] - \
        raw_stats["precpu_stats"]["cpu_usage"]["total_usage"]
    system_delta = raw_stats["cpu_stats"]["system_cpu_usage"] - \
        raw_stats["precpu_stats"]["system_cpu_usage"]

    cpu_percent = 0.0
    if system_delta > 0.0 and cpu_delta > 0.0:
        cpu_percent = (cpu_delta / system_delta) * \
            len(raw_stats["cpu_stats"]["cpu_usage"]["percpu_usage"]) * 100.0

    # Memory Usage
    mem_usage = raw_stats["memory_stats"]["usage"]
    mem_limit = raw_stats["memory_stats"]["limit"]
    mem_percent = (mem_usage / mem_limit) * 100.0 if mem_limit else 0.0

    # Network
    net_input = sum(net.get("rx_bytes", 0)
                    for net in raw_stats.get("networks", {}).values())
    net_output = sum(net.get("tx_bytes", 0)
                     for net in raw_stats.get("networks", {}).values())

    # Block I/O
    blkio_stats = raw_stats.get("blkio_stats", {}).get(
        "io_service_bytes_recursive", [])
    block_input = get_blkio_value(blkio_stats, "Read")
    block_output = get_blkio_value(blkio_stats, "Write")

    return {
        "cpu_percent": round(cpu_percent, 2),
        "mem_usage": bytes_to_mb(mem_usage),
        "mem_limit": bytes_to_mb(mem_limit),
        "mem_percent": round(mem_percent, 2),
        "net_input": bytes_to_mb(net_input),
        "net_output": bytes_to_mb(net_output),
        "block_input": bytes_to_mb(block_input),
        "block_output": bytes_to_mb(block_output)
    }


def find_compose_file(base_dir):
    for root, _, files in os.walk(base_dir):
        for fname in files:
            if fname in ('docker-compose.yml', 'docker-compose.yaml'):
                return os.path.join(root, fname)
    return None


def parse_docker_compose(compose_content):
    services = []

    try:
        compose_data = yaml.safe_load(compose_content)
        if 'services' in compose_data:
            for service_name, config in compose_data['services'].items():
                service_info = {
                    'name': service_name,
                    'ports': []
                }

                if 'ports' in config:
                    for port_mapping in config['ports']:
                        if isinstance(port_mapping, str):
                            host_port, container_port = port_mapping.split(':')
                            service_info['ports'].append({
                                'host': host_port.strip(),
                                'container': container_port.strip()
                            })
                        elif isinstance(port_mapping, dict):
                            service_info['ports'].append({
                                'host': port_mapping.get('published'),
                                'container': port_mapping.get('target')
                            })

                services.append(service_info)
    except yaml.YAMLError as e:
        print(f"YAML parsing error: {e}")

    return services


def get_container_details(container_name):
    container_info = {}
    container = client.containers.get(container_name)
    container_info['name'] = container.name
    container_info['status'] = container.status
    container_info['ports'] = container.attrs['NetworkSettings']['Ports']
    return container_info


def create_public_hostname(subdomain: str, port: int) -> tuple[bool, str]:
    hostname = f"{subdomain}.{CF_DOMAIN}"
    tunnel_url = f"http://{CF_IP}:{port}"

    tunnel_config_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{CF_TUNNEL_ID}/configurations"
    config_resp = requests.get(tunnel_config_url, headers=HEADERS)

    if config_resp.status_code != 200:
        return False, f"Failed to fetch tunnel config: {config_resp.text}"

    current_config = config_resp.json().get("result", {}).get("config", {})
    current_ingress = current_config.get("ingress", [])

    catch_all = None
    if current_ingress and current_ingress[-1].get("service", "").startswith("http_status"):
        catch_all = current_ingress.pop()

    for rule in current_ingress:
        if rule.get("hostname") == hostname:
            return True, f"https://{hostname} (already exists)"

    current_ingress.insert(0, {
        "hostname": hostname,
        "service": tunnel_url
    })

    if catch_all:
        current_ingress.append(catch_all)

    update_resp = requests.put(
        tunnel_config_url,
        headers=HEADERS,
        json={"config": {"ingress": current_ingress}}
    )
    if update_resp.status_code != 200:
        return False, f"Tunnel config update failed: {update_resp.text}"

    dns_url = f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records"
    existing = requests.get(
        f"{dns_url}?type=CNAME&name={hostname}", headers=HEADERS)

    if existing.status_code == 200 and existing.json().get("result"):
        return True, f"https://{hostname} (already exists)"

    cname_data = {
        "type": "CNAME",
        "name": hostname,
        "content": f"{CF_TUNNEL_ID}.cfargotunnel.com",
        "proxied": True
    }

    dns_resp = requests.post(dns_url, headers=HEADERS, json=cname_data)
    if dns_resp.status_code != 200:
        return False, f"DNS creation failed: {dns_resp.text}"

    return True, f"https://{hostname}"


@app.route('/')
def index():
    containers = client.containers.list(all=True)
    return render_template('index.html', containers=containers)


@app.route('/container/action', methods=['POST'])
def container_action():
    container_id = request.form.get('container_id')
    action = request.form.get('action')

    if not container_id:
        flash("Error: No container ID provided.", "danger")
        return redirect(url_for('index'))

    try:
        container = client.containers.get(container_id)

        if action == 'start':
            container.start()
        elif action == 'stop':
            container.stop()
        elif action == 'restart':
            container.restart()
        else:
            flash(f"Unknown action: {action}", "danger")
            return redirect(url_for('index'))

        flash(f"Container {action}ed successfully!", "success")

    except Exception as e:
        flash(f"Error performing action: {str(e)}", "danger")

    return redirect(url_for('index'))


@app.route('/upload_app', methods=['GET', 'POST'])
def upload_app():
    if request.method == 'POST':
        project_name = request.form.get('project_name')
        upload_method = request.form.get('upload_method')

        if not project_name or not upload_method:
            flash('Project name and upload method are required.', 'danger')
            return redirect(url_for('upload_app'))

        save_path = os.path.join(PROJECTS_DIR, project_name)

        try:
            if os.path.exists(save_path):
                shutil.rmtree(save_path)
            os.makedirs(save_path, exist_ok=True)

            if upload_method == 'upload':
                zip_file = request.files.get('zip_file')
                if not zip_file or zip_file.filename == '':
                    flash('No ZIP file uploaded.', 'danger')
                    return redirect(url_for('upload_app'))

                zip_path = os.path.join('/tmp', f"{project_name}.zip")
                zip_file.save(zip_path)
                shutil.unpack_archive(zip_path, save_path)
                os.remove(zip_path)

            elif upload_method == 'github':
                github_url = request.form.get('github_url')
                if not github_url:
                    flash('No GitHub URL provided.', 'danger')
                    return redirect(url_for('upload_app'))

                subprocess.run(
                    ["git", "clone", github_url, save_path], check=True)

            else:
                flash('Invalid upload method selected.', 'danger')
                return redirect(url_for('upload_app'))

            sub_items = os.listdir(save_path)
            if len(sub_items) == 1:
                first_item = os.path.join(save_path, sub_items[0])
                if os.path.isdir(first_item):
                    for item in os.listdir(first_item):
                        shutil.move(os.path.join(first_item, item), save_path)
                    os.rmdir(first_item)

            compose_file = None
            for root, _, files in os.walk(save_path):
                for f in files:
                    if f in ('docker-compose.yml', 'docker-compose.yaml'):
                        compose_file = os.path.join(root, f)
                        break
                if compose_file:
                    break

            if not compose_file:
                shutil.rmtree(save_path)
                flash(
                    'No docker-compose.yml or .yaml found in uploaded project.', 'danger')
                return redirect(url_for('upload_app'))

            flash(f"{project_name} uploaded successfully!", "success")
            return redirect(url_for('compose_editor', project_name=project_name))

        except subprocess.CalledProcessError as e:
            shutil.rmtree(save_path, ignore_errors=True)
            flash(f"GitHub clone error: {e}", 'danger')
            return redirect(url_for('upload_app'))

        except Exception as e:
            shutil.rmtree(save_path, ignore_errors=True)
            flash(f"Error during upload: {str(e)}", 'danger')
            return redirect(url_for('upload_app'))

    return render_template('uploadpage.html')


@app.route('/compose_editor/<project_name>', methods=['GET', 'POST'])
def compose_editor(project_name):
    temp_project_dir = os.path.join(PROJECTS_DIR, project_name)
    compose_file_path = find_compose_file(temp_project_dir)

    if request.method == 'GET':
        compose_found = compose_file_path is not None
        compose_content = None

        if compose_found:
            with open(compose_file_path, 'r') as file:
                compose_content = file.read()

                print("Compose path:", compose_file_path)
                print("Compose content:", compose_content)

        return render_template('compose_editor.html',
                               compose_found=compose_found,
                               compose_path=compose_file_path,
                               compose_content=compose_content,
                               project_name=project_name)

    if request.method == 'POST':
        compose_content = None

        if 'compose_content' in request.form:
            compose_content = request.form['compose_content']

        elif 'new_compose_file' in request.files:
            file = request.files['new_compose_file']
            if file and file.filename:
                compose_content = file.read().decode('utf-8')

        if not compose_content or not compose_file_path:
            flash('No Compose content or file path found.', 'danger')
            return redirect(request.url)

        with open(compose_file_path, 'w') as file:
            file.write(compose_content)

        flash('Docker Compose file saved successfully!', 'success')

        try:
            compose_dict = yaml.safe_load(compose_content)
            services = compose_dict.get("services", {})
            print("Parsed services:", services)

            nginx_service = services.get("server") or services.get("nginx")
            print("Nginx service:", nginx_service)
            host_port = None

            if nginx_service:
                ports = nginx_service.get("ports", [])
                for port_entry in ports:
                    if isinstance(port_entry, str) and ':' in port_entry:
                        host_port = port_entry.split(":")[0].strip()
                        break
            else:
                flash("No 'nginx' service found in docker-compose.yml.", 'danger')
                return redirect(request.url)

            if not host_port:
                flash("No valid port mapping found for 'nginx' service.", 'danger')
                return redirect(request.url)

        except Exception as e:
            flash(f'Error parsing docker-compose.yml: {str(e)}', 'danger')
            return redirect(request.url)

        try:
            subprocess.run(
                ['docker-compose', '-f', compose_file_path, 'up', '-d'],
                check=True,
                cwd=os.path.dirname(compose_file_path)
            )
            flash('App deployed successfully!', 'success')
            return redirect(url_for('launch_app_config', project_name=project_name, docker_port=host_port))

        except subprocess.CalledProcessError as e:
            flash(f'Error during deployment: {e}', 'danger')

        return render_template('compose_editor.html',
                               compose_found=True,
                               compose_path=compose_file_path,
                               compose_content=compose_content,
                               project_name=project_name)


@app.route('/launch_app_config/<project_name>', methods=['GET', 'POST'])
def launch_app_config(project_name):
    docker_port = request.args.get('docker_port', '')

    if request.method == 'POST':
        subdomain = request.form['subdomain'].lower().replace(" ", "-")
        docker_port = request.form['docker_port']

        success, result = create_public_hostname(
            subdomain=subdomain,
            port=docker_port
        )

        if success:
            public_url = f"https://{subdomain}.{CF_DOMAIN}"
            flash(f"Public URL available at {public_url}", 'success')
        else:
            flash(f"Failed to configure Cloudflare Tunnel: {result}", 'danger')

        return redirect(url_for('index'))

    return render_template('launchapp.html', project_name=project_name, docker_port=docker_port)


@app.route('/logs/<container_id>')
def container_logs(container_id):
    try:
        container = client.containers.get(container_id)
        logs = container.logs(tail=100).decode('utf-8')
        return render_template('logs.html', container=container, logs=logs)
    except Exception as e:
        flash(f"Error fetching logs: {str(e)}", "danger")
        return redirect(url_for('index'))


@app.route('/api/logs/<container_id>')
def get_logs(container_id):
    try:
        container = client.containers.get(container_id)
        logs = container.logs(tail=100).decode('utf-8')
        return jsonify({"logs": logs})
    except docker.errors.NotFound:
        return jsonify({"logs": "Container not found"}), 404


@app.route('/stats/<container_id>')
def container_stats(container_id):
    try:
        container = client.containers.get(container_id)
        stats = get_container_stats(container)
        return render_template("stats.html", container=container, stats=stats)
    except Exception as e:
        flash(f"Error fetching stats: {str(e)}", "danger")
        return redirect(url_for('index'))


@app.route('/api/stats')
def api_stats():
    stats_data = []
    for container in client.containers.list():
        try:
            stats = get_container_stats(container)
            stats["name"] = container.name
            stats_data.append(stats)
        except Exception as e:
            print(f"Error fetching stats for {container.name}: {e}")
            continue
    return jsonify(stats_data)


@app.route('/api/stats/<container_id>')
def api_container_stats(container_id):
    try:
        container = client.containers.get(container_id)
        return get_container_stats(container)
    except Exception as e:
        return {"error": str(e)}, 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
