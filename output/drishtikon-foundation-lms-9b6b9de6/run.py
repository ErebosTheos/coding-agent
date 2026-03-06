import uvicorn
import os
import logging
from src.main import app

# Configure logging for the production entry point
# This ensures that even before the FastAPI app is fully initialized,
# we have a way to track the startup process and catch early failures.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("run")

def main():
    """
    Main entry point for the application.
    Loads configuration from environment variables and starts the Uvicorn server.
    
    Environment Variables:
    - HOST: The network interface to bind to (default: 0.0.0.0)
    - PORT: The port to listen on (default: 8000)
    - DEBUG: If 'true', enables auto-reload for development (default: false)
    - WORKERS: Number of worker processes (default: 1)
    - FORWARDED_ALLOW_IPS: Trusted proxy IPs (default: 127.0.0.1)
    """
    # Load configuration from environment variables with safe defaults for production
    host = os.getenv("HOST", "0.0.0.0")
    port_str = os.getenv("PORT", "8000")
    
    try:
        port = int(port_str)
    except ValueError:
        logger.warning(f"Invalid PORT value '{port_str}', defaulting to 8000")
        port = 8000
        
    reload = os.getenv("DEBUG", "false").lower() == "true"
    workers_str = os.getenv("WORKERS", "1")
    
    try:
        workers = int(workers_str)
    except ValueError:
        logger.warning(f"Invalid WORKERS value '{workers_str}', defaulting to 1")
        workers = 1
    
    # Security fix: Use specific trusted IPs instead of '*' to prevent header spoofing
    forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
    
    logger.info(f"Initializing LMS Platform on {host}:{port}")
    logger.info(f"Configuration: reload={reload}, workers={workers}")
    
    # In development mode (reload=True), uvicorn.run expects an import string
    # In production mode, we use the app object directly if workers=1,
    # but for consistency we use the import string which supports multi-processing.
    try:
        uvicorn.run(
            "src.main:app",
            host=host,
            port=port,
            reload=reload,
            workers=workers,
            log_level="info",
            proxy_headers=True,
            forwarded_allow_ips=forwarded_allow_ips,
            timeout_keep_alive=5,
            limit_concurrency=1000
        )
    except Exception as e:
        logger.critical(f"Critical failure during server execution: {str(e)}")
        # Exit with error code to allow container orchestrators to handle the crash
        os._exit(1)

if __name__ == "__main__":
    # Prevent accidental execution of multiple event loops
    # and ensure the environment is correctly set up before starting.
    main()