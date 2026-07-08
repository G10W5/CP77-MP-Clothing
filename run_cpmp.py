from cpmp import PipelineWorker, ConfigManager
import queue
import threading

def run_worker():
    q = queue.Queue()
    
    def drain():
        while True:
            msg = q.get()
            if isinstance(msg, dict):
                print(msg.get('text', ''))
            else:
                print(msg)
                
    threading.Thread(target=drain, daemon=True).start()
    
    cfg_mgr = ConfigManager()
    
    worker = PipelineWorker(q, cfg_mgr)
    worker.run()

if __name__ == '__main__':
    run_worker()
