import subprocess,sys
from pathlib import Path
root=Path('.antigravity')
p=root/'antigravity_cli.py'
original=p.read_bytes()
mutations=[('stream','yield Delta(step.text_delta)','yield Delta("BROKEN")','test_completion_stream_usage_isolation_cleanup'),('rate','429 if re.search','418 if re.search','test_errors[rate-RateLimitError-agy-test-quota]')]
for name,before,after,test in mutations:
    try:
        p.write_text(original.decode().replace(before,after,1),encoding='utf-8')
        r=subprocess.run([sys.executable,'-m','pytest',str(root/'tests/test_antigravity.py')+'::'+test,'-q','--basetemp='+str(root/'logs'/('mutation-temp-'+name))],capture_output=True,text=True)
        (root/'logs'/('mutation-'+name+'.log')).write_text(r.stdout+r.stderr,encoding='utf-8')
        print(name,'pytest exit',r.returncode,'KILLED' if r.returncode==1 else 'UNEXPECTED')
        assert r.returncode==1
    finally:
        p.write_bytes(original)
