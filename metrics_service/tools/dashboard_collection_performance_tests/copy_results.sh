#!/bin/bash
set -euo pipefail

# Usage:
#   INSTANCE_IP=instance_ip ./copy_results.sh

"===== Create 'results' directory if it doesn't exist ====="
mkdir -p ./results

echo "===== Copy collection results ====="
scp -r "ansible@$INSTANCE_IP:/home/ansible/results_scale1_internal.txt" ./results
scp -r "ansible@$INSTANCE_IP:/home/ansible/results_scale2_internal.txt" ./results
scp -r "ansible@$INSTANCE_IP:/home/ansible/results_scale3_internal.txt" ./results
scp -r "ansible@$INSTANCE_IP:/home/ansible/results_scale4_internal.txt" ./results

echo ""
echo "===== Copy API results ====="
scp -r "ansible@$INSTANCE_IP:/home/ansible/results_scale1_api.txt" ./results
scp -r "ansible@$INSTANCE_IP:/home/ansible/results_scale2_api.txt" ./results
scp -r "ansible@$INSTANCE_IP:/home/ansible/results_scale3_api.txt" ./results
scp -r "ansible@$INSTANCE_IP:/home/ansible/results_scale4_api.txt" ./results

echo ""
echo "===== Copy API SLO results ====="
scp -r "ansible@$INSTANCE_IP:/home/ansible/results_scale1_api_slo.txt" ./results
scp -r "ansible@$INSTANCE_IP:/home/ansible/results_scale2_api_slo.txt" ./results
scp -r "ansible@$INSTANCE_IP:/home/ansible/results_scale3_api_slo.txt" ./results
scp -r "ansible@$INSTANCE_IP:/home/ansible/results_scale4_api_slo.txt" ./results

echo ""
echo "DONE."
