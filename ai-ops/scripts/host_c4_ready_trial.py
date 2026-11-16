"""한 foreground 채널에서 실제 안내와 ready 실행기를 함께 중계한다.

stdout review_notice_requested의 arguments를 실제 native 도구로 전송한 뒤
그 성공 응답만 stdin에 전달한다. 이 CLI는 도구 성공이나 사람 리뷰를 생성하지 않는다.
기존 run_c4_ready_trial.py와 같은 인자를 사용하며 새 실행별 승인 파일도 동일하게 요구한다.
"""
from pathlib import Path
import sys

from run_c4_ready_trial import parse_args
from ai_ops.c_trial_notice_host import host_ready
from ai_ops.c_trial_ready import ReadyTrialPlan
from ai_ops.c_trial_bundle import verify_ready_runtime
from ai_ops.c_trial_guards import private_json


def main(argv=None):
    if not __debug__:
        raise ValueError('검증을 제거하는 최적화 모드는 사용할 수 없습니다')
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    intent = ReadyTrialPlan.model_validate(private_json(args.intent))
    verify_ready_runtime(root,intent)
    return host_ready(args,argv,root=root,
                      python=sys.executable,host_input=sys.stdin.buffer,output=sys.stdout)


if __name__ == '__main__':
    raise SystemExit(main())
