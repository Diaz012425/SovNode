import psutil
import json
from datetime import datetime

def measure_cpu_load():
    cpu_percent = psutil.cpu_percent(interval=1)
    timestamp = datetime.now().isoformat()
    return {'timestamp': timestamp, 'cpu_percent': cpu_percent}

if __name__ == '__main__':
    with open('log.json', 'w') as f:
        json.dump(measure_cpu_load(), f)