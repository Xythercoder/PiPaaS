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

CLOUDFLARE_ZONE_ID = os.getenv('CLOUDFLARE_ZONE_ID')
CLOUDFLARE_API_KEY = os.getenv('CLOUDFLARE_API_KEY')
CLOUDFLARE_TUNNEL_ID = os.getenv('CLOUDFLARE_TUNNEL_ID')
CLOUDFLARE_ACCOUNT_ID = os.getenv('CLOUDFLARE_ACCOUNT_ID')
CLOUDFLARE_CLIENT_ID = os.getenv('CLOUDFLARE_CLIENT_ID')
CLOUDFLARE_CLIENT_SECRET = os.getenv('CLOUDFLARE_CLIENT_SECRET')

CLOUDFLARE_API_URL = "https://api.cloudflare.com/client/v4"


PROJECTS_DIR = os.path.join(os.getcwd(), 'user_projects')
os.makedirs(PROJECTS_DIR, exist_ok=True)


def bytes_to_mb(bytes_value):
    """Converts bytes to megabytes."""
    return f"{bytes_value / (1024 * 1024):.2f} MB" if bytes_value is not None else "N/A"


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
    # Custom function to get container details using Docker SDK or CLI
    container_info = {}
    container = client.containers.get(container_name)
    container_info['name'] = container.name
    container_info['status'] = container.status
    container_info['ports'] = container.attrs['NetworkSettings']['Ports']
    return container_info


def update_cloudflare_tunnel(project_name, subdomain, docker_port):
    headers = {
        'Authorization': f'Bearer {CLOUDFLARE_API_KEY}',
        'Content-Type': 'application/json',
    }

    # Create public hostname using Cloudflare Tunnel API
    data = {
        "tunnel": CLOUDFLARE_TUNNEL_ID,
        "url": f"http://localhost:{docker_port}",
        "hostname": f"{subdomain}.{CLOUDFLARE_ZONE_ID}",
    }

    # Send the request to Cloudflare
    response = requests.post(
        f"{CLOUDFLARE_API_URL}/zones/{CLOUDFLARE_ZONE_ID}/dns_records",
        json=data,
        headers=headers
    )

    if response.status_code == 200:
        flash(
            f"Public hostname {subdomain}.{CLOUDFLARE_ZONE_ID} created successfully!", 'success')
    else:
        flash(f"Error creating Cloudflare tunnel: {response.text}", 'danger')


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
        project_name = request.form['project_name']
        upload_method = request.form['upload_method']
        save_path = os.path.join(PROJECTS_DIR, project_name)

        try:
            if os.path.exists(save_path):
                shutil.rmtree(save_path)
            os.makedirs(save_path, exist_ok=True)

            if upload_method == 'upload':
                zip_file = request.files.get('zip_file')
                if not zip_file:
                    flash('No ZIP file uploaded', 'danger')
                    return redirect(url_for('upload_app'))

                zip_path = os.path.join('/tmp', f"{project_name}.zip")
                zip_file.save(zip_path)
                shutil.unpack_archive(zip_path, save_path)
                os.remove(zip_path)

            elif upload_method == 'github':
                github_url = request.form.get('github_url')
                if not github_url:
                    flash('No GitHub URL provided', 'danger')
                    return redirect(url_for('upload_app'))

                subprocess.run(
                    ["git", "clone", github_url, save_path], check=True)

            flash(f"{project_name} uploaded successfully!", "success")
            return redirect(url_for('compose_editor', project_name=project_name))

        except Exception as e:
            flash(f"Error: {str(e)}", "danger")
            return redirect(url_for('upload_app'))

    return render_template('uploadpage.html')



@app.route('/compose_editor/<project_name>', methods=['GET', 'POST'])
def compose_editor(project_name):
    temp_project_dir = os.path.join(PROJECTS_DIR, project_name)
    compose_file_path = os.path.join(temp_project_dir, 'docker-compose.yml')

    if request.method == 'GET':
        compose_found = os.path.exists(compose_file_path)
        if compose_found:
            compose_path = compose_file_path
            with open(compose_file_path, 'r') as file:
                compose_content = file.read()
        else:
            compose_path = None
            compose_content = None 

        return render_template('compose_editor.html',
                               compose_found=compose_found,
                               compose_path=compose_path,
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

        if not compose_content:
            flash('No Compose content provided. Please fill or upload.', 'danger')
            return redirect(request.url)

        with open(compose_file_path, 'w') as file:
            file.write(compose_content)

        flash('Docker Compose file saved successfully!', 'success')

        try:
            subprocess.run(
                ['docker-compose', '-f', compose_file_path, 'up', '-d'],
                check=True,
                cwd=temp_project_dir
            )
            flash('App deployed successfully!', 'success')
            return redirect(url_for('launch_app_config', project_name=project_name))

        except subprocess.CalledProcessError as e:
            flash(f'Error during deployment: {e}', 'danger')


        return render_template('compose_editor.html',
                               compose_found=True,
                               compose_path=compose_file_path,
                               compose_content=compose_content,
                               project_name=project_name)



@app.route('/launch_app_config/<project_name>', methods=['GET', 'POST'])
def launch_app_config(project_name):
    if request.method == 'POST':
        subdomain = request.form['subdomain']
        docker_port = request.form['docker_port']

        update_cloudflare_tunnel(project_name, subdomain, docker_port)

        return redirect(url_for('launch_app_config', project_name=project_name))

    return render_template('launchapp.html', project_name=project_name)


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
