import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional
from structured_logger import get_logger


logger = get_logger("notification-system-health")

class HealthProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health/live':
            self._handle_probe(self.server.liveness_check_fn, "alive")
        elif self.path == '/health/ready':
            self._handle_probe(self.server.readiness_check_fn, "ready")
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_probe(self, check_fn: Optional[Callable[[], bool]], success_status: str):
        try:
            # Se la callback non è definita, assumiamo che il controllo sia superato
            is_ok = check_fn() if check_fn else True
            
            if is_ok:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                response = {"status": success_status}
                self.wfile.write(json.dumps(response).encode('utf-8'))
            else:
                self.send_response(503)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                response = {"status": "unavailable", "detail": "Health check failed"}
                self.wfile.write(json.dumps(response).encode('utf-8'))
        except Exception as e:
            self.send_response(503)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = {"status": "error", "detail": str(e)}
            self.wfile.write(json.dumps(response).encode('utf-8'))

    def log_message(self, format, *args):
        # Disabilitato per evitare che i ping di Kubernetes inondino i log del container
        pass

class ProbeHTTPServer(HTTPServer):
    def __init__(self, server_address, RequestHandlerClass, liveness_check_fn=None, readiness_check_fn=None):
        super().__init__(server_address, RequestHandlerClass)
        self.liveness_check_fn = liveness_check_fn
        self.readiness_check_fn = readiness_check_fn

class HealthServer:
    def __init__(self, port: int = 8080, 
                 liveness_check_fn: Optional[Callable[[], bool]] = None, 
                 readiness_check_fn: Optional[Callable[[], bool]] = None):
        self.port = port
        self.server = ProbeHTTPServer(
            ('', self.port), 
            HealthProbeHandler,
            liveness_check_fn=liveness_check_fn,
            readiness_check_fn=readiness_check_fn
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        logger.info("Health probe server started", extra={"port": self.port})

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        logger.info("Health probe server stopped")
