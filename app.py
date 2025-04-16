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

# CLOUDFLARE_API_URL = "https://api.cloudflare.com/client/v4"
TUNNEL_HOST = f"{CF_TUNNEL_ID}.cfargotunnel.com"


PROJECTS_DIR = os.path.join(os.getcwd(), 'user_projects')
os.makedirs(PROJECTS_DIR, exist_ok=True)


def bytes_to_mb(bytes_value):
    """Converts bytes to megabytes."""
    return f"{bytes_value / (1024 * 1024):.2f} MB" if bytes_value is not None else "N/A"


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
                        # Support both formats: "8000:8000" or long format
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


def create_cloudflare_public_hostname(subdomain, port):
    try:
        full_hostname = f"{subdomain}.{CF_DOMAIN}"
        headers = {
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "application/json"
        }

        # Step 1: Update Tunnel Ingress Config
        tunnel_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{CF_TUNNEL_ID}/configurations"
        tunnel_payload = {
            "config": {
                "ingress": [
                    {
                        "hostname": full_hostname,
                        "service": f"http://{CF_IP}:{port}",
                        "originRequest": {}
                    },
                    {
                        "service": "http_status:404"
                    }
                ]
            }
        }

        tunnel_resp = requests.put(
            tunnel_url, headers=headers, json=tunnel_payload)
        if not tunnel_resp.ok:
            return False, f"Ingress config failed: {tunnel_resp.json()}"

        dns_list_url = f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records?type=CNAME&name={full_hostname}"
        dns_list_resp = requests.get(dns_list_url, headers=headers)
        if not dns_list_resp.ok:
            return False, f"Failed to check DNS records: {dns_list_resp.json()}"

        existing_records = dns_list_resp.json().get("result", [])
        if existing_records:
            record_id = existing_records[0]["id"]

            update_url = f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records/{record_id}"
            update_payload = {
                "type": "CNAME",
                "name": full_hostname,
                "content": TUNNEL_HOST,
                "proxied": True
            }
            update_resp = requests.put(
                update_url, headers=headers, json=update_payload)
            if not update_resp.ok:
                return False, f"DNS update failed: {update_resp.json()}"
        else:

            dns_create_url = f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records"
            create_payload = {
                "type": "CNAME",
                "name": full_hostname,
                "content": TUNNEL_HOST,
                "proxied": True
            }
            create_resp = requests.post(
                dns_create_url, headers=headers, json=create_payload)
            if not create_resp.ok:
                return False, f"DNS creation failed: {create_resp.json()}"

        return True, full_hostname

    except Exception as e:
        return False, str(e)


@app.route('/')
def index():
    containers = client.containers.list(all=True)
    return render_template('index.html', containers=containers)


@app.route('/api/container/<action>', methods=['POST'])
def container_action(action):
    # Get container ID from the request JSON
    container_id = request.json.get('container_id')
    if not container_id:
        return jsonify({"success": False, "message": "No container ID provided."}), 400

    try:
        container = client.containers.get(container_id)
        if action == "stop":
            container.stop()
        elif action == "restart":
            container.restart()
        elif action == "start":
            container.start()
        else:
            return jsonify({"success": False, "message": "Invalid action."}), 400

        return jsonify({"success": True, "message": f"Container {action}ed successfully."})
    except docker.errors.NotFound:
        return jsonify({"success": False, "message": "Container not found."}), 404
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


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
            first_service = next(iter(services.values()), {})
            ports = first_service.get("ports", [])

            host_port = None
            for port_entry in ports:
                if isinstance(port_entry, str) and ':' in port_entry:
                    host_port = port_entry.split(":")[0]
                    break

            if not host_port:
                flash('No valid port mapping found in Compose file.', 'danger')
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

        success, result = create_cloudflare_public_hostname(
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
        return render_template('logs.html', logs=logs, container_id=container_id)
    except docker.errors.NotFound:
        return jsonify({"success": False, "message": "Container not found."}), 404
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


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
        stats = container.stats(stream=False)
        return render_template('stats.html', stats=stats, container_id=container_id)
    except docker.errors.NotFound:
        return jsonify({"success": False, "message": "Container not found."}), 404
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/stats')
def api_stats():
    stats_data = []
    for container in client.containers.list():
        try:
            stat = container.stats(stream=False)

            # CPU Usage %
            cpu_delta = stat["cpu_stats"]["cpu_usage"]["total_usage"] - \
                stat["precpu_stats"]["cpu_usage"]["total_usage"]
            system_delta = stat["cpu_stats"]["system_cpu_usage"] - \
                stat["precpu_stats"]["system_cpu_usage"]
            cpu_percent = 0.0
            if system_delta > 0.0 and cpu_delta > 0.0:
                cpu_percent = (cpu_delta / system_delta) * \
                    len(stat["cpu_stats"]["cpu_usage"]["percpu_usage"]) * 100.0

            # Memory usage
            mem_usage = stat["memory_stats"]["usage"]
            mem_limit = stat["memory_stats"]["limit"]
            mem_percent = (mem_usage / mem_limit) * 100.0 if mem_limit else 0.0

            stats_data.append({
                "name": container.name,
                "cpu_percent": f"{cpu_percent:.2f}",
                "mem_usage": bytes_to_mb(mem_usage),
                "mem_limit": bytes_to_mb(mem_limit),
                "mem_percent": f"{mem_percent:.2f}",
                "net_input": bytes_to_mb(stat["networks"]["eth0"]["rx_bytes"]) if "eth0" in stat["networks"] else "N/A",
                "net_output": bytes_to_mb(stat["networks"]["eth0"]["tx_bytes"]) if "eth0" in stat["networks"] else "N/A",
                "block_input": bytes_to_mb(stat["blkio_stats"]["io_service_bytes_recursive"][0]["value"]) if stat["blkio_stats"]["io_service_bytes_recursive"] else "N/A",
                "block_output": bytes_to_mb(stat["blkio_stats"]["io_service_bytes_recursive"][1]["value"]) if len(stat["blkio_stats"]["io_service_bytes_recursive"]) > 1 else "N/A"
            })
        except Exception as e:
            print(f"Error fetching stats for {container.name}: {e}")
            continue

    return jsonify(stats_data)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
