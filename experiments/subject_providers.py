"""Provider adapters for the same experimental subject, memory and search contracts."""
from collections import deque
from types import SimpleNamespace
import os
from openai import OpenAI
from remi.providers import GPTModel,providers_from_environment
from remi.settings import private_key
from experiments.subject_maintenance import maintain
from experiments.subject_extraction import propose as extract_memories
from experiments.event_maintenance import provider_plan


class JsonTransport:
    def __init__(self,client):
        self.transport=client
        self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
    def create(self,**request):
        wire={key:value for key,value in request.items() if key not in {'max_completion_tokens','response_format'}}
        wire.update(max_tokens=request['max_completion_tokens'],response_format={'type':'json_object'},extra_body={'thinking':{'type':'disabled'}})
        return self.transport.chat.completions.create(**wire)
    def close(self):self.transport.close()


class SubjectTextModel(GPTModel):
    def __init__(self,model,api_key,base_url,timeout=45):
        if model not in {'gpt-4o-mini','deepseek-flash'}:raise ValueError('experiment_model_required')
        self.model=model;self.timeout=timeout;self.calls=deque(maxlen=4096)
        client=OpenAI(api_key=api_key,base_url=base_url,max_retries=0,timeout=timeout)
        self.client=JsonTransport(client) if model=='deepseek-flash' else client
    def maintain_subjects(self,state,*,deadline):return maintain(self,state,deadline=deadline)[0]
    def extract_memories(self,state,*,deadline):return extract_memories(self,state,deadline=deadline)
    def propose(self,state,*,deadline):
        return provider_plan(self,state,deadline=deadline)


def experimental_providers(model,deepseek_key_file=None):
    old,embedder,reranker=providers_from_environment();old.close()
    if model=='deepseek-flash':
        from pathlib import Path
        key=Path(deepseek_key_file).read_text().strip();url='https://api.deepseek.com'
    else:key=private_key('LLM_API_KEY','LLM_API_KEY_FILE');url=os.getenv('LLM_BASE_URL','https://api.quickrouter.us/v1')
    return SubjectTextModel(model,key,url,float(os.getenv('REMI_LLM_TIMEOUT','45'))),embedder,reranker
