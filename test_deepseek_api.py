"""DeepSeek 调用层的离线测试：python3 test_deepseek_api.py

用假的 OpenAI client，不依赖网络，也不会真的调用 DeepSeek API。
"""
import io
import os
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

os.environ.setdefault('DEEPSEEK_API_KEY', 'dummy-for-tests')
from openai.types import Model
from openai.types.chat import ChatCompletion
import feed_health as H
import translate_rss as T

T.time.sleep = lambda s: None


class FakeClient:
    def __init__(self, models=None, models_error=None, reply='译文', usage=None, error=None):
        self.requests = []
        outer = self

        class _Models:
            def list(self_inner):
                if models_error:
                    raise models_error
                return type('Page', (), {'data': [Model.model_validate(m) for m in models or []]})()

        class _Completions:
            def create(self_inner, **kwargs):
                outer.requests.append(kwargs)
                if error:
                    raise error
                return ChatCompletion.model_validate({
                    'id': 'x', 'object': 'chat.completion', 'created': 0, 'model': kwargs['model'],
                    'choices': [{'index': 0, 'finish_reason': 'stop',
                                 'message': {'role': 'assistant', 'content': reply}}],
                    'usage': usage or {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140,
                                       'prompt_cache_hit_tokens': 60,
                                       'completion_tokens_details': {'reasoning_tokens': 0}},
                })

        self.models = _Models()
        self.chat = type('Chat', (), {'completions': _Completions()})()


def model(id_, name):
    return {'id': id_, 'object': 'model', 'owned_by': 'deepseek', 'created': 0, 'name': name}


def use(client):
    T.client = client
    T.USAGE = T.ApiUsage()
    T.MODEL, T.MODEL_NAME = T.DEFAULT_MODEL, None
    return client


def test_resolve_model_prefers_unversioned_flash():
    use(FakeClient(models=[model('deepseek-v4-pro', 'DeepSeek-V4-Pro'),
                           model('deepseek-flash', 'DeepSeek-V4.1-Flash')]))
    with redirect_stdout(io.StringIO()):
        assert T.resolve_model() == ('deepseek-flash', 'DeepSeek-V4.1-Flash')


def test_resolve_model_falls_back_to_any_flash_then_default():
    use(FakeClient(models=[model('deepseek-v5-pro', 'P'), model('deepseek-v5-flash', 'DeepSeek-V5-Flash')]))
    with redirect_stdout(io.StringIO()):
        assert T.resolve_model() == ('deepseek-v5-flash', 'DeepSeek-V5-Flash')
    use(FakeClient(models_error=RuntimeError('503')))
    with redirect_stdout(io.StringIO()):
        assert T.resolve_model() == ('deepseek-flash', None)       # 查不到就用默认名，照常翻译


def test_every_call_disables_thinking_and_uses_resolved_model():
    c = use(FakeClient(models=[model('deepseek-flash', 'DeepSeek-V4.1-Flash')], reply='短标题'))
    with redirect_stdout(io.StringIO()):
        T.resolve_model()
    cache = T.TranslationCache({})
    T._current.feed = 'somepics'
    assert T.translate_with_deepseek('<p>Hello</p>') == '短标题'
    assert T.summarize_title_text('A very long description of a photo', cache) == '短标题'
    assert len(c.requests) == 2
    for r in c.requests:
        assert r['model'] == 'deepseek-flash'
        assert r['extra_body'] == {'thinking': {'type': 'disabled'}}
    assert [r['max_tokens'] for r in c.requests] == [8192, 100]


def test_usage_is_recorded_per_feed_with_cache_hits_and_reasoning():
    use(FakeClient(usage={'prompt_tokens': 1000, 'completion_tokens': 500, 'total_tokens': 1500,
                          'prompt_cache_hit_tokens': 400, 'completion_tokens_details': {'reasoning_tokens': 300}}))
    T._current.feed = 'DaringFireball'
    T.translate_with_deepseek('<p>One</p>')
    T.translate_with_deepseek('<p>Two</p>')
    r = T.USAGE.by_feed['DaringFireball']
    assert (r['calls'], r['prompt'], r['cache_hit'], r['completion'], r['reasoning']) == (2, 2000, 800, 1000, 600)
    # 缓存命中 800 × 0.02 + 未命中 1200 × 1 + 输出 1000 × 4，单位元/百万 tokens
    assert abs(T.ApiUsage.cost(r) - (800 * 0.02 + 1200 * 1 + 1000 * 4) / 1e6) < 1e-12
    out = io.StringIO()
    with redirect_stdout(out):
        T.USAGE.report()
    assert '2 次调用' in out.getvalue() and '推理 600' in out.getvalue() and 'DaringFireball' in out.getvalue()


def test_failures_are_counted_and_translation_falls_back():
    use(FakeClient(error=RuntimeError('Error code: 402 - Insufficient Balance')))
    T._current.feed = 'nicenews'
    with redirect_stdout(io.StringIO()):
        assert T.translate_with_deepseek('<p>Hi</p>') is None          # 不写缓存，本次用原文
    t = T.USAGE.totals()
    assert (t['calls'], t['failures']) == (1, 1) and 'Insufficient Balance' in T.USAGE.last_error


T0 = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def api(calls=10, failures=0, name='DeepSeek-V4.1-Flash', err=''):
    return {'model': 'deepseek-flash', 'model_name': name, 'calls': calls, 'failures': failures, 'last_error': err}


def kinds(state):
    return [a['kind'] for a in state['alerts'] if a['feed'] == '*' and a['kind'] != 'enabled']


def test_model_change_is_announced_once():
    s = H.update(None, [], {}, T0, api())
    assert kinds(s) == []                                         # 第一次只记录，不通知
    s = H.update(s, [], {}, T0 + timedelta(hours=5), api())
    assert kinds(s) == []
    s = H.update(s, [], {}, T0 + timedelta(hours=10), api(name='DeepSeek-V4.2-Flash'))
    assert kinds(s) == ['model-changed']
    assert 'V4.1-Flash → DeepSeek-V4.2-Flash' in s['alerts'][0]['title']
    s = H.update(s, [], {}, T0 + timedelta(hours=15), api(name='DeepSeek-V4.2-Flash'))
    assert kinds(s) == ['model-changed']
    s = H.update(s, [], {}, T0 + timedelta(hours=20), api(calls=0, name=None))   # 查模型失败：不误报
    assert kinds(s) == ['model-changed']


def test_api_all_failing_alerts_after_two_runs_and_recovers():
    s = H.update(None, [], {}, T0, api())
    s = H.update(s, [], {}, T0 + timedelta(hours=1), api(calls=8, failures=3))   # 部分失败：抖动，不算
    s = H.update(s, [], {}, T0 + timedelta(hours=2), api(calls=8, failures=8, err='402 Insufficient Balance'))
    assert kinds(s) == []
    s = H.update(s, [], {}, T0 + timedelta(hours=3), api(calls=0))                # 全部命中缓存：没有信息
    assert s['api']['fail_runs'] == 1
    s = H.update(s, [], {}, T0 + timedelta(hours=4), api(calls=5, failures=5, err='402 Insufficient Balance'))
    assert kinds(s) == ['api-failing'] and '402 Insufficient Balance' in s['alerts'][0]['body']
    s = H.update(s, [], {}, T0 + timedelta(hours=8), api(calls=5, failures=5))
    assert kinds(s) == ['api-failing']                                            # 7 天内不重复提醒
    s = H.update(s, [], {}, T0 + timedelta(days=8), api(calls=5, failures=5))
    assert kinds(s) == ['api-failing', 'api-failing'] and '第 2 次提醒' in s['alerts'][0]['title']
    s = H.update(s, [], {}, T0 + timedelta(days=8, hours=5), api(calls=5, failures=1))
    assert kinds(s)[0] == 'api-recovered' and s['api']['incident'] is None


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} 项全部通过 ✅")
