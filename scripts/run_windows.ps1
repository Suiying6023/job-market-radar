$ErrorActionPreference = "Stop"
python -m pip install -e .
job-radar doctor --config config.yaml
job-radar collect --config config.yaml
job-radar export --config config.yaml
job-radar report --config config.yaml
