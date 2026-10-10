# Set the Tokenizer for TPS metrics 4/10
export TOKENIZER="JerroldK/H4-14b-contract-extractor-adapter"

echo "Starting SageMaker endpoint benchmark..."

./awscurl -c 7 -N 5 -X POST \
  -n sagemaker https://runtime.sagemaker.us-east-1.amazonaws.com/endpoints/lmi-Hermes-FP8-2026-07-08-08-20-13-904/invocations-response-stream \
  --connect-timeout 900 \
  --dataset prompts \
  -H 'Content-Type: application/json' \
  -H 'X-Amzn-SageMaker-Inference-Component: adapter-lmi-Hermes-FP8-2026-07-08-08-20-13-904' \
  -P -t -o output.txt > benchmark_summary.json

echo "Benchmark complete! Summary is saved in benchmark_summary.txt, and raw responses are saved in output.txt.* files."