import os
import redis
import requests
import json
import sys
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
from urllib3.util import Retry

# Configuration
REDIS_HOST = os.getenv('REDIS_HOST', 'redis')
REDIS_PORT = int(os.getenv('REDIS_PORT', '6379'))
JOB_QUEUE = os.getenv('JOB_QUEUE', 'ranges')
SUCCESS_QUEUE = os.getenv('SUCCESS_QUEUE', 'succeeded')
FAILED_QUEUE = os.getenv('FAILED_QUEUE', 'failed')
PROGRESS_QUEUE = os.getenv('PROGRESS_QUEUE', 'in_progress')
METRICS = os.getenv('METRICS', 'metrics')
WORKER_PREFIX = os.getenv('WORKER_PREFIX', 'stellar-core')
NAMESPACE = os.getenv('NAMESPACE', 'default')
WORKER_COUNT = int(os.getenv('WORKER_COUNT', 3))
LOGGING_INTERVAL_SECONDS = int(os.getenv('LOGGING_INTERVAL_SECONDS', 10))

s = requests.Session()
retries = Retry(total=1)
s.mount('http://', requests.adapters.HTTPAdapter(max_retries=retries))

def get_logging_level():
    name_to_level = {
        'CRITICAL': logging.CRITICAL,
        'ERROR': logging.ERROR,
        'WARNING': logging.WARNING,
        'INFO': logging.INFO,
        'DEBUG': logging.DEBUG,
    }
    result = name_to_level.get(os.getenv('LOGGING_LEVEL', 'INFO'))
    if result is not None:
        return result
    else:
        return logging.INFO

# Initialize Redis client
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

# Configure logging
log_file_name = f"job_monitor_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')}.log"
log_file_path = os.path.join('/data', log_file_name)
logging.basicConfig(level=get_logging_level(), format='%(asctime)s - %(levelname)s - %(message)s', handlers=[
    logging.StreamHandler(sys.stdout),
    logging.FileHandler(log_file_path),
])
logger = logging.getLogger()

# In-memory status data structure and threading lock
status = {
    'num_remain': 1, # initialize the job remaining to non-zero to indicate something is running, just the status hasn't been updated yet
    'num_succeeded': 0,
    'num_failed': 0,
    'num_in_progress': 0,
    'jobs_failed': [],
    'jobs_in_progress': [],
    'workers': []
}
status_lock = threading.Lock()

metrics = {
    'metrics': []
}
metrics_lock = threading.Lock()

class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/status':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            with status_lock:
                self.wfile.write(json.dumps(status).encode())
        # Prometheus metrics use status data
        elif self.path == '/prometheus':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            status_data = None
            prometheus_metrics = ''
            with status_lock:
                status_data = status
            for metric in status_data.keys():
                if metric.startswith('num_'):
                    prometheus_metrics += f'ssc_parallel_catchup_jobs{{queue="{metric}"}} {status_data[metric]}\n'
                if metric == 'workers_refresh_duration':
                    prometheus_metrics += f'ssc_parallel_catchup_workers_refresh_duration_seconds {status_data[metric]}\n'
                elif metric.startswith('workers_'):
                    prometheus_metrics += f'ssc_parallel_catchup_workers{{status="{metric}"}} {status_data[metric]}\n'
            self.wfile.write(prometheus_metrics.encode())
        elif self.path == '/metrics':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            with metrics_lock:
                self.wfile.write(json.dumps(metrics).encode())
        else:
            self.send_response(404)
            self.end_headers()

def retry_jobs_in_progress():
    while redis_client.llen(PROGRESS_QUEUE) > 0:
        job = redis_client.lmove(PROGRESS_QUEUE, JOB_QUEUE, "RIGHT", "LEFT")
        logger.info("moved job %s from %s to %s", job, PROGRESS_QUEUE, JOB_QUEUE)

def update_status_and_metrics():
    global status
    while True:
        try:
            # Ping each worker status
            worker_statuses = []
            all_workers_down = True
            workers_down = 0
            workers_up = 0
            logger.info("Starting worker liveness check")
            workers_refresh_start_time = time.time()
            for i in range(WORKER_COUNT):
                worker_name = f"{WORKER_PREFIX}-{i}.{WORKER_PREFIX}.{NAMESPACE}.svc.cluster.local"
                try:
                    response = requests.get(f"http://{worker_name}:11626/info", timeout=10)
                    logger.debug("Worker %s is running, status code %d, response: %s", worker_name, response.status_code, response.json())
                    worker_statuses.append({'worker_id': i, 'status': 'running', 'info': response.json()['info']['status']})
                    workers_up += 1
                    all_workers_down = False
                except requests.exceptions.RequestException:
                    logger.info("Worker %s is down", worker_name)
                    worker_statuses.append({'worker_id': i, 'status': 'down'})
                    workers_down += 1
            workers_refresh_duration = time.time() - workers_refresh_start_time
            logger.info("Finished workers liveness check")
            # Retry stuck jobs
            if all_workers_down and redis_client.llen(PROGRESS_QUEUE) > 0:
                logger.info("all workers are down but some jobs are stuck in progress")
                logger.info("moving them from %s to %s queue", PROGRESS_QUEUE, JOB_QUEUE)
                retry_jobs_in_progress()

            # Check the queue status
            # For remaining and successful jobs, we just print their count, do not care what they are and who owns it
            logger.info("Getting status data from redis")
            num_remain = redis_client.llen(JOB_QUEUE)
            num_succeeded = redis_client.llen(SUCCESS_QUEUE)
            # For failed and in-progress jobs, we retrieve their full content
            jobs_failed = redis_client.lrange(FAILED_QUEUE, 0, -1)
            num_failed = len(jobs_failed)
            jobs_in_progress = redis_client.lrange(PROGRESS_QUEUE, 0, -1)
            num_in_progress = len(jobs_in_progress)

            # update the status
            with status_lock:
                logger.info("Updating status data structure inside lock")
                status = {
                    'num_remain': num_remain,
                    'num_succeeded': num_succeeded,
                    'num_failed': num_failed,
                    'num_in_progress': num_in_progress,
                    'jobs_failed': jobs_failed,
                    'jobs_in_progress': jobs_in_progress,
                    'workers': worker_statuses,
                    'workers_up': workers_up,
                    'workers_down': workers_down,
                    'workers_refresh_duration': workers_refresh_duration,
                }
            #logger.info("Status: %s", json.dumps(status))

            # update the metrics
            logger.info("Checking for metrics in redis")
            new_metrics = redis_client.spop(METRICS, 1000)
            if len(new_metrics) > 0:
                logger.info("New metrics: %s", json.dumps(new_metrics))
                with metrics_lock:
                    metrics['metrics'].extend(new_metrics)
            #logger.info("Metrics: %s", json.dumps(metrics))

        except Exception as e:
            logger.error("Error while getting status: %s", str(e))

        logger.info("Sleeping...")
        time.sleep(LOGGING_INTERVAL_SECONDS)

def run(server_class=HTTPServer, handler_class=RequestHandler):
    server_address = ('', 8080)
    httpd = server_class(server_address, handler_class)
    logger.info('Starting httpd server...')
    httpd.serve_forever()

if __name__ == '__main__':
    log_thread = threading.Thread(target=update_status_and_metrics)
    log_thread.daemon = True
    log_thread.start()

    run()
