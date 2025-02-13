"""
这个文件应该只被 /app/views.py 依赖
依赖于 /app/utils.py, /app/scheduler_func.py,

scheduler_func 依赖于 wechat_send 依赖于 utils

文件中参数存在 activity 的函数需要在 transaction.atomic() 块中进行。
如果存在预期异常，抛出 ActivityException，否则抛出其他异常
"""
from datetime import datetime, timedelta
from app.utils import get_person_or_org, if_image
from app.notification_utils import(
    notification_create,
    bulk_notification_create,
    notification_status_change,
)
from app.models import (
    NaturalPerson,
    Position,
    Organization,
    OrganizationType,
    Position,
    Activity,
    TransferRecord,
    Participant,
    Notification,
    ModifyOrganization,
    Comment,
    CommentPhoto,
    YQPointDistribute,
    ActivityPhoto
)
import qrcode
import os
from boottest.hasher import MySHA256Hasher
from boottest.settings import MEDIA_ROOT, MEDIA_URL
from boottest import local_dict
from django.core.files import File
from django.core.files.base import ContentFile
import io
import base64
from django.db.models import Sum
from app.scheduler import scheduler
from app.scheduler_func import changeActivityStatus, notifyActivity

hash_coder = MySHA256Hasher(local_dict["hash"]["base_hasher"])

def get_activity_QRcode(activity):

    auth_code = hash_coder.encode(str(activity.id))
    # url = f"http://localhost:8000/checkinActivity/{activity.id}?auth={auth_code}"
    url = os.path.join(
            local_dict["url"]["login_url"], 
            "checkinActivity", 
            f"{activity.id}?auth={auth_code}"
        )

    qr=qrcode.QRCode(version = 2,error_correction = qrcode.constants.ERROR_CORRECT_L,box_size=5,border=5)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image()
    io_buffer = io.BytesIO()
    img.save(io_buffer, "png")
    data = base64.encodebytes(io_buffer.getvalue()).decode()
    return "data:image/png;base64," + str(data)

class  ActivityException(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return self.msg


# 时间合法性的检查，检查时间是否在当前时间的一个月以内，并且检查开始的时间是否早于结束的时间，
def check_ac_time(start_time, end_time):
    now_time = datetime.now()
    month_late = now_time + timedelta(days=30)
    if not start_time < end_time:
        return False
    if now_time < start_time < month_late:
        return True  # 时间所处范围正确

    return False


def activity_base_check(request, edit=False):

    context = dict()

    # title, introduction, location 创建时不能为空
    context["title"] = request.POST["title"]
    context["introduction"] = request.POST["introduction"]
    context["location"] = request.POST["location"]
    assert len(context["title"]) > 0
    assert len(context["introduction"]) > 0
    assert len(context["location"]) > 0

    # url
    context["url"] = request.POST["URL"]

    # 预算，元气值支付模式，是否直接向学院索要元气值
    # 在审核通过后，这些不可修改
    context["budget"] = float(request.POST["budget"])
    signscheme = int(request.POST["signscheme"])
    if signscheme:
        context["bidding"] = True
    else:
        context["bidding"] = False

    # 向学院申请元气值
    from_college = request.POST["from_college"]
    if from_college == "1":
        context["from_college"] = True
    elif from_college == "0":
        context["from_college"] = False


    # examine_teacher 需要特殊检查
    context["examine_teacher"] = request.POST.get("examine_teacher")
    # 申请理由
    context["apply_reason"] = request.POST.get("apply_reason", "")
    if context["from_college"]:
        assert len(context["apply_reason"]) > 0

    # 预报备
    context["recorded"] = False
    if request.POST.get("recorded"):
        context["recorded"] = True

    # 时间
    act_start = datetime.strptime(request.POST["actstart"], "%Y-%m-%d %H:%M")  # 活动报名时间
    act_end = datetime.strptime(request.POST["actend"], "%Y-%m-%d %H:%M")  # 活动报名结束时间
    context["start"] = act_start
    context["end"] = act_end
    assert check_ac_time(act_start, act_end)

    # create 或者调整报名时间，都是要确保活动不要立刻截止报名
    now_time = datetime.now()
    if not edit or request.POST.get("adjust_apply_ddl"):
        prepare_scheme = int(request.POST["prepare_scheme"])
        prepare_times = Activity.EndBeforeHours.prepare_times
        prepare_time = prepare_times[prepare_scheme]
        signup_end = act_start - timedelta(hours=prepare_time)
        assert now_time <= signup_end
        context["endbefore"] = prepare_scheme
        context["signup_end"] = signup_end
    else:
        # 修改但不调整报名截止时间，后面函数自己查
        context["adjust_apply"] = False

    # 人数限制
    capacity = request.POST.get("maxpeople")
    no_limit = request.POST.get("unlimited_capacity")
    if no_limit is not None:
        capacity = 10000
    if capacity is not None and capacity != "":
        capacity = int(capacity)
        assert capacity >= 0
    context["capacity"] = capacity

    # 需要签到
    if request.POST.get("need_checkin"):
        context["need_checkin"] = True

    # 价格
    aprice = float(request.POST["aprice"])
    assert int(aprice * 10) / 10 == aprice
    assert aprice >= 0
    context["aprice"] = aprice


    # 图片 优先使用上传的图片
    announcephoto = request.FILES.getlist("images")
    if len(announcephoto) > 0:
        pic = announcephoto[0]
        if if_image(pic)!=2:
            raise ActivityException("上传的附件只支持图片格式。")
    else:
        if request.POST.get("picture1"):
            pic = request.POST.get("picture1")
        elif request.POST.get("picture2"):
            pic = request.POST.get("picture2")
        elif request.POST.get("picture3"):
            pic = request.POST.get("picture3")
        elif request.POST.get("picture4"):
            pic = request.POST.get("picture4")
        else:
            pic = request.POST.get("picture5")


    if not edit:
        assert pic is not None

    context["pic"] = pic


    return context


def create_activity(request):

    context = activity_base_check(request)

    # 审批老师存在
    examine_teacher = NaturalPerson.objects.get(name=context["examine_teacher"])
    assert examine_teacher.identity == NaturalPerson.Identity.TEACHER

    # 检查完毕，创建活动
    org = get_person_or_org(request.user, "Organization")
    activity = Activity.objects.create(
                    title=context["title"], organization_id=org,
                    examine_teacher=examine_teacher
                )
    activity.title = context["title"]
    activity.introduction = context["introduction"]
    activity.location = context["location"]
    activity.capacity = context["capacity"]
    activity.URL = context["url"]
    activity.budget = context["budget"]
    activity.start = context["start"]
    activity.end = context["end"]
    activity.YQPoint = context["aprice"]
    activity.bidding = context["bidding"]
    activity.apply_end = context["signup_end"]
    activity.apply_reason = context["apply_reason"]
    if context["from_college"]:
        activity.source = Activity.YQPointSource.COLLEGE
    activity.endbefore = context["endbefore"]
    if context.get("need_checkin"):
        activity.need_checkin = True
    if context["recorded"]:
        # 预报备活动，先开放报名，再审批
        activity.recorded = True
        activity.status = Activity.Status.APPLYING
        notifyActivity(activity.id, "newActivity")

        scheduler.add_job(notifyActivity, "date", id=f"activity_{activity.id}_remind",
            run_date=activity.start - timedelta(minutes=15), args=[activity.id, "remind"], replace_existing=True)
        # 活动状态修改
        scheduler.add_job(changeActivityStatus, "date", id=f"activity_{activity.id}_{Activity.Status.WAITING}", 
            run_date=activity.apply_end, args=[activity.id, Activity.Status.APPLYING, Activity.Status.WAITING])
        scheduler.add_job(changeActivityStatus, "date", id=f"activity_{activity.id}_{Activity.Status.PROGRESSING}", 
            run_date=activity.start, args=[activity.id, Activity.Status.WAITING, Activity.Status.PROGRESSING])
        scheduler.add_job(changeActivityStatus, "date", id=f"activity_{activity.id}_{Activity.Status.END}", 
            run_date=activity.end, args=[activity.id, Activity.Status.PROGRESSING, Activity.Status.END])
    
    activity.save()

    ActivityPhoto.objects.create(image=context["pic"], type=ActivityPhoto.PhotoType.ANNOUNCE ,activity=activity)

    notification_create(
        receiver=examine_teacher.person_id,
        sender=request.user,
        typename=Notification.Type.NEEDDO,
        title=Notification.Title.VERIFY_INFORM,
        content="您有一个活动待审批",
        URL=f"/examineActivity/{activity.id}",
        relate_instance=activity,
    )

    return activity.id



def modify_activity(request, activity):

    if activity.status == Activity.Status.REVIEWING:
        modify_reviewing_activity(request, activity)
    elif activity.status == Activity.Status.APPLYING or activity.status == Activity.Status.WAITING:
        modify_accepted_activity(request, activity)
    else:
        raise ValueError



"""
检查 修改审核中活动 的 request
审核中，只需要修改内容，不需要通知
但如果修改了审核老师，需要再通知新的审核老师，并 close 原审核请求
"""
def modify_reviewing_activity(request, activity):

    context = activity_base_check(request, edit=True)

    """
    不允许修改审批老师
    if context["examine_teacher"] == activity.examine_teacher.name:
        pass
    else:
        examine_teacher = NaturalPerson.objects.get(name=context["examine_teacher"])
        assert examine_teacher.identity == NaturalPerson.Identity.TEACHER
        activity.examine_teacher = examine_teacher
        # TODO
        # 修改审核记录，通知老师 

        notification = Notification.objects.get(relate_instance=activity, status=Notification.Status.UNDONE)
        notification_status_change(notification, Notification.Status.DELETE)

        notification_create(
            receiver=examine_teacher.person_id,
            sender=request.user,
            typename=Notification.Type.NEEDDO,
            title=Notification.Title.VERIFY_INFORM,
            content="您有一个活动待审批",
            URL=f"/examineActivity/{activity.id}",
            relate_instance=activity,
        )
    """


    if context.get("adjust_apply") is not None:
        # 注意这里是不调整
        assert context["adjust_apply"] == False
        assert activity.apply_end <= context["start"] - timedelta(hours=1)
    else:
        activity.apply_end = context["signup_end"]

    activity.title = context["title"]
    activity.introduction = context["introduction"]
    activity.location = context["location"]
    activity.capacity = context["capacity"]
    activity.URL = context["url"]
    activity.budget = context["budget"]
    activity.start = context["start"]
    activity.end = context["end"]
    activity.YQPoint = context["aprice"]
    activity.bidding = context["bidding"]
    activity.apply_reason = context["apply_reason"]
    if context["from_college"]:
        activity.source = Activity.YQPointSource.COLLEGE
    if context.get("need_checkin"):
        activity.need_checkin = True
    else:
        activity.need_checkin = False
    activity.save()

    # 图片
    if context["pic"] is not None:
        pic = activity.photos.get(type=ActivityPhoto.PhotoType.ANNOUNCE)
        pic.image = context["pic"]
        pic.save()



"""
对已经通过审核的活动进行修改
不能修改预算，元气值支付模式，审批老师
只能修改时间，地点，URL, 简介，向同学收取元气值时的元气值数量

# 这个实际上应该是 activated/valid activity
"""
def modify_accepted_activity(request, activity):

    # TODO
    # 删除任务，注册新任务

    to_participants = [f"您参与的活动{activity.title}发生变化"]
    to_subscribers = [f"您关注的活动{activity.title}发生变化"]
    if activity.location != request.POST["location"]:
        to_participants.append("活动地点修改为" + activity.location)
        activity.location = request.POST["location"]

    # 不是学院来源时，价格可能会变
    if activity.source != Activity.YQPointSource.COLLEGE:
        aprice = float(request.POST["aprice"])
        assert int(aprice * 10) / 10 == aprice
        assert aprice >= 0
        if activity.YQPoint != aprice:
            to_subscribers.append("活动价格调整为" + str(aprice))
            to_participants.append("活动价格调整为" + str(aprice))
            activity.YQPoint = aprice

    # 时间改变
    act_start = datetime.strptime(request.POST["actstart"], "%Y-%m-%d %H:%M")
    now_time = datetime.now()
    assert now_time < act_start

    if request.POST.get("adjust_apply_ddl"):
        prepare_scheme = int(request.POST["prepare_scheme"])
        prepare_times = Activity.EndBeforeHours.prepare_times
        prepare_time = prepare_times[prepare_scheme]
        signup_end = act_start - timedelta(hours=prepare_time)
        assert now_time <= signup_end
        activity.apply_end = signup_end
        to_subscribers.append(f"活动报名截止时间调整为{signup_end.strftime('%Y-%m-%d %H:%M')}")
        to_participants.append(f"活动报名截止时间调整为{signup_end.strftime('%Y-%m-%d %H:%M')}")
    else:
        signup_end = activity.apply_end
        assert signup_end + timedelta(hours=1) < act_start
    
    if activity.start != act_start:
        to_subscribers.append(f"活动开始时间调整为{act_start.strftime('%Y-%m-%d %H:%M')}")
        to_participants.append(f"活动开始时间调整为{act_start.strftime('%Y-%m-%d %H:%M')}")
        activity.start = act_start

    if signup_end < now_time and activity.status == Activity.Status.WAITING:
        activity.status = Activity.Status.APPLYING


    if request.POST.get("unlimited_capacity"):
        capacity = 10000
    else:
        capacity = int(request.POST["maxpeople"])
        assert capacity > 0
        if capacity < len(Participant.objects.filter(
            activity_id=activity.id, 
            status=Participant.AttendStatus.APLLYSUCCESS
        )):
            raise ActivityException(f"当前成功报名人数已超过{capacity}人")
    activity.capacity = capacity

    if request.POST.get("need_checkin"):
        activity.need_checkin = True
    else:
        activity.need_checkin = False

    activity.end = datetime.strptime(request.POST["actend"], "%Y-%m-%d %H:%M")
    assert activity.start < activity.end
    activity.URL = request.POST["URL"]
    activity.introduction = request.POST["introduction"]
    activity.save()


    if activity.status == Activity.Status.APPLYING:
        scheduler.add_job(changeActivityStatus, "date", id=f"activity_{activity.id}_{Activity.Status.WAITING}", 
            run_date=activity.apply_end, args=[activity.id, Activity.Status.APPLYING, Activity.Status.WAITING], replace_existing=True)
    scheduler.add_job(notifyActivity, "date", id=f"activity_{activity.id}_remind",
        run_date=activity.start - timedelta(minutes=15), args=[activity.id, "remind"], replace_existing=True)
    scheduler.add_job(changeActivityStatus, "date", id=f"activity_{activity.id}_{Activity.Status.PROGRESSING}", 
        run_date=activity.start, args=[activity.id, Activity.Status.WAITING, Activity.Status.PROGRESSING], replace_existing=True)
    scheduler.add_job(changeActivityStatus, "date", id=f"activity_{activity.id}_{Activity.Status.END}", 
        run_date=activity.end, args=[activity.id, Activity.Status.PROGRESSING, Activity.Status.END], replace_existing=True)


    if len(to_subscribers) > 1:
        notifyActivity(activity.id, "modification_sub_ex_par", "\n".join(to_subscribers))
    if len(to_participants) > 1:   
        notifyActivity(activity.id, "modification_par", "\n".join(to_participants))



def accept_activity(request, activity):

    # 审批通过
    activity.valid = True

    # 通知
    notification = Notification.objects.get(
        relate_instance=activity, 
        status=Notification.Status.UNDONE,
        title=Notification.Title.VERIFY_INFORM
    )
    notification_status_change(notification, Notification.Status.DONE)

    notification_create(
        receiver=activity.organization_id.organization_id,
        sender=request.user,
        typename=Notification.Type.NEEDREAD,
        title=Notification.Title.ACTIVITY_INFORM,
        content=f"您的活动{activity.title}已通过审批。",
        URL=f"/viewActivity/{activity.id}",
        relate_instance=activity,
    )

    if activity.status == Activity.Status.REVIEWING:

        activity.status = Activity.Status.APPLYING

        notifyActivity(activity.id, "newActivity")
        scheduler.add_job(notifyActivity, "date", id=f"activity_{activity.id}_remind",
            run_date=activity.start - timedelta(minutes=15), args=[activity.id, "remind"], replace_existing=True)
        # 活动状态修改
        scheduler.add_job(changeActivityStatus, "date", id=f"activity_{activity.id}_{Activity.Status.WAITING}", 
            run_date=activity.apply_end, args=[activity.id, Activity.Status.APPLYING, Activity.Status.WAITING])
        scheduler.add_job(changeActivityStatus, "date", id=f"activity_{activity.id}_{Activity.Status.PROGRESSING}", 
            run_date=activity.start, args=[activity.id, Activity.Status.WAITING, Activity.Status.PROGRESSING])
        scheduler.add_job(changeActivityStatus, "date", id=f"activity_{activity.id}_{Activity.Status.END}", 
            run_date=activity.end, args=[activity.id, Activity.Status.PROGRESSING, Activity.Status.END])


    # 向学院申请元气值时，审批通过后转账
    if activity.source == Activity.YQPointSource.COLLEGE and activity.YQPoint > 0:
        organization_id = activity.organization_id_id
        organization = Organization.objects.select_for_update().get(id=organization_id)
        YP = Organization.objects.select_for_update().get(oname="元培学院")
        organization.YQPoint += activity.YQPoint
        YP.YQPoint -= activity.YQPoint
        amount = activity.YQPoint
        record = TransferRecord.objects.create(
            proposer=YP.organization_id, recipient=organization.organization_id
        )
        record.amount = amount
        record.message = f"From College"
        record.status = TransferRecord.TransferStatus.ACCEPTED
        record.time = str(datetime.now())
        record.corres_act = activity
        record.save()
        YP.save()
        organization.save()

    activity.save()



def reject_activity(request, activity):
    # 审批通过
    activity.valid = True

    # 通知
    notification = Notification.objects.get(
        relate_instance=activity, 
        status=Notification.Status.UNDONE,
        title=Notification.Title.VERIFY_INFORM
    )
    notification_status_change(notification, Notification.Status.DONE)

    notification_create(
        receiver=activity.organization_id.organization_id,
        sender=request.user,
        typename=Notification.Type.NEEDREAD,
        title=Notification.Title.ACTIVITY_INFORM,
        content=f"您的活动{activity.title}被拒绝。",
        URL=f"/viewActivity/{activity.id}",
        relate_instance=activity,
    )

    if activity.status == Activity.Status.REVIEWING:
        activity.status = Activity.Status.REJECT
    else:
        Notification.objects.filter(
            relate_instance=activity
            ).update(status=Notification.Status.DELETE)
        Participant.objects.filter(
                activity_id=activity
            ).update(status=Participant.AttendStatus.APLLYFAILED)
        notifyActivity(activity.id, "modification_par", f"您报名的活动{activity.title}已取消。")
        activity.status = Activity.Status.CANCELED
        scheduler.remove_job(f"activity_{activity.id}_remind")
        scheduler.remove_job(f"activity_{activity.id}_{Activity.Status.WAITING}")
        scheduler.remove_job(f"activity_{activity.id}_{Activity.Status.PROGRESSING}")
        scheduler.remove_job(f"activity_{activity.id}_{Activity.Status.END}")

    activity.save()

# 调用的时候用 try
# 调用者把 activity_id 作为参数传过来
def applyActivity(request, activity):
    context = dict()
    context["success"] = False
    CREATE = True

    payer = NaturalPerson.objects.select_for_update().get(
        person_id=request.user
    )
    try:
        participant = Participant.objects.select_for_update().get(
            activity_id=activity, person_id=payer
        )
        CREATE = False
    except:
        pass
    if CREATE == False:
        assert participant.status == Participant.AttendStatus.CANCELED


    organization_id = activity.organization_id_id
    organization = Organization.objects.select_for_update().get(id=organization_id)
    YP = Organization.objects.select_for_update().get(oname="元培学院")


    if activity.source == Activity.YQPointSource.COLLEGE:
        if not activity.bidding:
            if activity.current_participants < activity.capacity:
                activity.current_participants += 1
            else:
                raise ActivityException("活动已报满，请稍后再试。")
        else:
            activity.current_participants += 1
    else:
        """
        存在投点的逻辑，暂时不用
        if not activity.bidding:
            amount = float(activity.YQPoint)
            if activity.current_participants < activity.capacity:
                activity.current_participants += 1
            else:
                raise ActivityException("活动已报满，请稍后再试。")
        else:
            amount = float(request.POST["willingness"])
            if not activity.YQPoint <= amount <= activity.YQPoint * 3:
                raise ActivityException("投点范围为基础值的 1-3 倍")
            # 依然增加，此时current_participants统计的是报名的人数，是可以比总人数多的
            activity.current_participants += 1
            assert amount == int(amount * 10) / 10
        """
        amount = float(activity.YQPoint)

        if not payer.YQPoint + payer.quota >= amount:
            raise ActivityException(f"没有足够的元气值。您当前的元气值数量为 {payer.YQPoint + payer.quota}")

        if activity.bidding:
            activity.current_participants += 1
        else:
            if activity.current_participants < activity.capacity:
                activity.current_participants += 1
            else:
                raise ActivityException("活动已报满，请稍后再试。")

        use_quota = amount
        if payer.quota >= amount:
            payer.quota -= amount
            amount = 0
        else:
            use_quota = payer.quota
            amount -= payer.quota
            payer.quota = 0
            payer.YQPoint -= amount
        YP.YQPoint -= use_quota
        # 用配额的部分
        if use_quota > 0:
            record = TransferRecord.objects.create(
                proposer=request.user, recipient=organization.organization_id
            )
            record.amount = use_quota
            record.message = "quota"
            organization.YQPoint += use_quota
            record.status = TransferRecord.TransferStatus.ACCEPTED
            record.time = str(datetime.now())
            record.corres_act = activity
            record.save()
        # 用个人账户的部分
        if amount > 0:
            record = TransferRecord.objects.create(
                proposer=request.user, recipient=organization.organization_id
            )
            record.amount = amount
            record.message = "YQPoint"
            organization.YQPoint += amount
            record.status = TransferRecord.TransferStatus.ACCEPTED
            record.time = str(datetime.now())
            record.corres_act = activity
            record.save()

    if CREATE:
        participant = Participant.objects.create(
            activity_id=activity, person_id=payer
        )
    if not activity.bidding:
        participant.status = Participant.AttendStatus.APLLYSUCCESS
    else:
        participant.status = Participant.AttendStatus.APPLYING

    YP.save()
    organization.save()
    participant.save()
    payer.save()
    activity.save()


def cancel_activity(request, activity):

    if activity.status == Activity.Status.REVIEWING:
        activity.status = Activity.Status.ABORT
        activity.save()
        # 修改老师的通知
        notification = Notification.objects.get(
            relate_instance=activity, 
            status=Notification.Status.UNDONE
        )
        notification_status_change(notification, Notification.Status.DELETE)
        return

    if activity.status == Activity.Status.PROGRESSING:
        if activity.start.day == datetime.now().day and datetime.now() < activity.start + timedelta(days=1):
            pass
        else:
            raise ActivityException("活动已于一天前开始，不能取消。")

    org = Organization.objects.select_for_update().get(
                organization_id=request.user
            )
    assert activity.organization_id == org


    if activity.status != Activity.Status.REVIEWING and activity.YQPoint > 0:
        if activity.source == Activity.YQPointSource.COLLEGE:
            if org.YQPoint < activity.YQPoint:
                raise ActivityException("没有足够的元气值退还给学院，不能取消。")
            org.YQPoint -= activity.YQPoint
            # 这里加个悲观锁能提高性能吗 ？
            YP = Organization.objects.select_for_update().get(oname="元培学院")
            YP.YQPoint += activity.YQPoint
            YP.save()
            # activity 上了悲观锁，这里不用锁，如果锁了整个 record 表全锁住
            record = TransferRecord.objects.get(
                proposer=YP, status=TransferRecord.TransferStatus.ACCEPTED, corres_act=activity
            )
            record.status = TransferRecord.TransferStatus.REFUND
            record.save()
        else:
            # 同理，这里也不用上锁
            records = TransferRecord.objects.filter(
                status=TransferRecord.TransferStatus.ACCEPTED, corres_act=activity
            )
            total_amount = records.aggregate(nums=Sum('amount'))["nums"]
            if total_amount is None:
                total_amount = 0.0
            if total_amount > org.YQPoint:
                raise ActivityException("没有足够的元气值退还给同学，不能取消。")
            totalQuota = records.filter(message="quota").aggregate(nums=Sum('amount'))["nums"]
            YP = Organization.objects.select_for_update().get(oname="元培学院")
            YP.YQPoint += activity.YQPoint
            YP.save()
            for record in records:
                payer = NaturalPerson.objects.select_for_update().get(person_id=record.proposer)
                if record.message == "quota":
                    payer.quota += record.amount
                    YP.YQPoint += record.amount
                else:
                    payer.YQPoint += record.amount
                payer.save()
                record.status = TransferRecord.TransferStatus.REFUND
                record.save()

            org.YQPoint -= total_amount


    activity.status = Activity.Status.CANCELED
    notifyActivity(activity.id, "modification_par", f"您报名的活动{activity.title}已取消。")
    notification = Notification.objects.get(
        relate_instance=activity,
        typename=Notification.Type.NEEDDO
    )
    notification_status_change(notification, Notification.Status.DELETE)

    participants = Participant.objects.filter(
            activity_id=activity
        ).update(status=Participant.AttendStatus.APLLYFAILED)

    scheduler.remove_job(f"activity_{activity.id}_remind")
    scheduler.remove_job(f"activity_{activity.id}_{Activity.Status.WAITING}")
    scheduler.remove_job(f"activity_{activity.id}_{Activity.Status.PROGRESSING}")
    scheduler.remove_job(f"activity_{activity.id}_{Activity.Status.END}")

    org.save()
    activity.save()



def withdraw_activity(request, activity):

    np = NaturalPerson.objects.select_for_update().get(person_id=request.user)
    participant = Participant.objects.select_for_update().get(
        activity_id=activity,
        person_id=np,
        status__in=[
            Participant.AttendStatus.APPLYING,
            Participant.AttendStatus.APLLYSUCCESS,
        ],
    )
    org = Organization.objects.select_for_update().get(
        organization_id=activity.organization_id.organization_id
    )
    YP = Organization.objects.select_for_update().get(oname="元培学院")
    participant.status = Participant.AttendStatus.CANCELED
    activity.current_participants -= 1

    if activity.source == Activity.YQPointSource.STUDENT and activity.YQPoint > 0:
        record = None
        half_refund = 1
        if activity.bidding:
            assert activity.status == Activity.Status.APPLYING
        if activity.status == Activity.Status.WAITING:
            if not datetime.now() < activity.start - timedelta(hours=1):
                raise ActivityException("活动即将开始，不能取消。")
            half_refund = 0.5

        # 使用 quota 交付的记录
        try:
            record = TransferRecord.objects.select_for_update().get(
                corres_act=activity,
                proposer=request.user,
                status=TransferRecord.TransferStatus.ACCEPTED,
                message="quota"
            )
        except:
            pass
        if record is not None:
            amount = record.amount * half_refund
            amount = int(10 * amount) * 0.1
            record.status = TransferRecord.TransferStatus.REFUND
            np.quota += amount
            YP.YQPoint += amount
            if org.YQPoint < amount:
                raise ActivityException("组织账户元气值不足，请与组织负责人联系。")
            org.YQPoint -= amount
            record.save()

        # 使用个人账户交付的记录
        record = None
        try:
            record = TransferRecord.objects.select_for_update().get(
                corres_act=activity,
                proposer=request.user,
                status=TransferRecord.TransferStatus.ACCEPTED,
                message="YQPoint"
            )
        except:
            pass
        if record is not None:
            amount = record.amount * half_refund
            amount = int(10 * amount) * 0.1
            record.status = TransferRecord.TransferStatus.REFUND
            np.YQPoint += amount
            if org.YQPoint < amount:
                raise ActivityException("组织账户元气值不足，请与组织负责人联系。")
            org.YQPoint -= amount
            record.save()


    YP.save()
    org.save()
    participant.save()
    np.save()
    activity.save()









