#!/bin/bash
# AXELR AI - Test Runner

echo "╔══════════════════════════════════════════════════╗"
echo "║     AXELR AI - PROVIDER TEST SUITE v1.0         ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "⚠️  .env file not found. Creating from .env.example..."
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "✅ Created .env from .env.example"
        echo "⚠️  Please edit .env with your API keys first!"
        exit 1
    else
        echo "❌ .env.example not found. Please create .env manually."
        exit 1
    fi
fi

echo "1. Quick Diagnostic Test (requires server running)"
echo "   python quick_diagnose.py [--server http://localhost:8000]"
echo ""
echo "2. Full Provider Validation (imports from app.py)"
echo "   python test_providers.py"
echo ""
echo "3. Stress Test (customize requests/concurrency)"
echo "   python stress_test_providers.py [--requests 10] [--concurrency 3]"
echo ""
echo "4. Continuous Monitor (customize interval)"
echo "   python monitor_providers.py [--interval 30]"
echo ""
echo "5. Run all tests sequentially (except monitor)"
echo "   python quick_diagnose.py && python test_providers.py && python stress_test_providers.py"
echo ""

# Ask user which test to run
read -p "Select test (1-5): " choice

case $choice in
    1)
        python quick_diagnose.py
        ;;
    2)
        python test_providers.py
        ;;
    3)
        python stress_test_providers.py
        ;;
    4)
        python monitor_providers.py
        ;;
    5)
        echo "Running all tests..."
        python quick_diagnose.py && python test_providers.py && python stress_test_providers.py
        ;;
    *)
        echo "Invalid choice"
        ;;
esac