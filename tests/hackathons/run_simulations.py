"""
Axelr Hackathon Simulation Runner
=================================
One-click script to start all local simulation servers and run the complete
end-to-end test suite for PayPal, Vultr, and hybrid execution workflows.
"""
import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("axelr_simulations.log")
    ]
)
logger = logging.getLogger("simulation.runner")

# Configuration
SIMULATION_SERVERS = [
    {
        "name": "paypal-mock",
        "script": "test_paypal_simulation.py",
        "port": 9000,
        "cmd": [sys.executable, "test_paypal_simulation.py"]
    },
    {
        "name": "vultr-mock",
        "script": "test_vultr_simulation.py",
        "port": 9001,
        "cmd": [sys.executable, "test_vultr_simulation.py"]
    }
]

async def wait_for_service(url: str, timeout: int = 30) -> bool:
    """Wait for a service to become available."""
    import httpx
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=2.0)
                if response.status_code < 500:
                    return True
        except Exception:
            await asyncio.sleep(1)
    return False

async def main():
    """Run the complete simulation suite."""
    logger.info("=" * 70)
    logger.info("AXELR HACKATHON SIMULATION RUNNER")
    logger.info("=" * 70)
    
    # Change to script directory
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    
    processes: List[subprocess.Popen] = []
    
    try:
        # Step 1: Start all mock servers
        logger.info("\n[1/4] Starting simulation servers...")
        for server in SIMULATION_SERVERS:
            logger.info(f"  Starting {server['name']} on port {server['port']}...")
            
            proc = subprocess.Popen(
                server["cmd"],
                cwd=str(script_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            processes.append(proc)
            
        # Wait for servers to start
        await asyncio.sleep(5)
        
        # Verify services are up
        logger.info("\n[2/4] Verifying service availability...")
        paypal_ok = await wait_for_service("http://localhost:9000/docs")
        vultr_ok = await wait_for_service("http://localhost:9001/docs")
        
        if not paypal_ok or not vultr_ok:
            logger.error("❌ Failed to start one or more simulation servers")
            if not paypal_ok:
                logger.error("   PayPal mock server failed to start")
            if not vultr_ok:
                logger.error("   Vultr mock server failed to start")
            raise RuntimeError("Simulation infrastructure unavailable")
            
        logger.info("✅ All mock servers are running!")
        
        # Step 3: Run PayPal test suite
        logger.info("\n[3/4] Running PayPal integration tests...")
        paypal_test_proc = subprocess.run(
            [sys.executable, "test_paypal_simulation.py", "test"],
            cwd=str(script_dir),
            capture_output=True,
            text=True
        )
        
        if paypal_test_proc.returncode != 0:
            logger.error("❌ PayPal tests failed!")
            logger.error(f"STDOUT: {paypal_test_proc.stdout}")
            logger.error(f"STDERR: {paypal_test_proc.stderr}")
        else:
            logger.info("✅ PayPal tests passed!")
            
        # Run Vultr test suite
        logger.info("\nRunning Vultr integration tests...")
        vultr_test_proc = subprocess.run(
            [sys.executable, "test_vultr_simulation.py", "test"],
            cwd=str(script_dir),
            capture_output=True,
            text=True
        )
        
        if vultr_test_proc.returncode != 0:
            logger.error("❌ Vultr tests failed!")
            logger.error(f"STDOUT: {vultr_test_proc.stdout}")
            logger.error(f"STDERR: {vultr_test_proc.stderr}")
        else:
            logger.info("✅ Vultr tests passed!")
            
        # Step 4: Run hybrid end-to-end tests
        logger.info("\n[4/4] Running hybrid end-to-end simulation...")
        hybrid_test_proc = subprocess.run(
            [sys.executable, "test_hybrid_simulation.py"],
            cwd=str(script_dir),
            capture_output=True,
            text=True
        )
        
        if hybrid_test_proc.returncode != 0:
            logger.error("❌ Hybrid simulation failed!")
            logger.error(f"STDOUT: {hybrid_test_proc.stdout}")
            logger.error(f"STDERR: {hybrid_test_proc.stderr}")
        else:
            logger.info("✅ Hybrid simulation completed successfully!")
            
        # Summary
        all_passed = (
            paypal_test_proc.returncode == 0 and
            vultr_test_proc.returncode == 0 and
            hybrid_test_proc.returncode == 0
        )
        
        logger.info("\n" + "=" * 70)
        logger.info("SIMULATION SUMMARY")
        logger.info("=" * 70)
        
        if all_passed:
            logger.info("🎉 ALL TESTS PASSED! Axelr is ready for hackathon submission.")
            logger.info("   PayPal: ✅ PASS")
            logger.info("   Vultr: ✅ PASS")
            logger.info("   Hybrid: ✅ PASS")
        else:
            logger.error("⚠️  Some tests failed. Review the logs above.")
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"Simulation runner failed: {str(e)}", exc_info=True)
    finally:
        # Cleanup processes
        logger.info("\nCleaning up simulation servers...")
        for proc in processes:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        logger.info("All servers terminated.")

if __name__ == "__main__":
    asyncio.run(main())