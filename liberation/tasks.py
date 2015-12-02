import json
import hashlib
import logging
import mailbox
import os
import random
import string
import tarfile
import time
from datetime import datetime
from shutil import rmtree

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import urlresolvers
from django.db import IntegrityError, transaction
from django.utils import safestring
from django.utils.translation import ugettext as _

from celery import chain, chord
from pytz import utc
from async_messages import message_user

from inboxen.celery import app
from inboxen.models import Email, Inbox
from liberation import utils
from inboxen import tasks

log = logging.getLogger(__name__)

TAR_TYPES = {
    '0': {'ext': 'tar.gz', 'writer': 'w:gz', 'mime-type': 'application/x-gzip'},
    '1': {'ext': 'tar.bz2', 'writer': 'w:bz2', 'mime-type': 'application/x-bzip2'},
    '2': {'ext': 'tar', 'writer': 'w:', 'mime-type': 'application/x-tar'}
}


@app.task(rate_limit='4/h')
def liberate(user_id, options):
    """ Get set for liberation, expects User object """
    options['user'] = user_id
    user = get_user_model().objects.get(id=user_id)
    lib_status = user.liberation

    tar_type = TAR_TYPES[options.get('compression_type', '0')]

    rstr = ""
    for i in range(7):
        rstr += string.ascii_letters[random.randint(0, 50)]
    basename = "%s_%s_%s_%s" % (time.time(), os.getpid(), rstr, hashlib.sha256(user.username + rstr).hexdigest()[:50])
    path = os.path.join(settings.LIBERATION_PATH, basename)
    tarname = "%s.%s" % (basename, tar_type["ext"])

    # Is this safe enough?
    try:
        os.mkdir(path, 0700)
    except (IOError, OSError), error:
        log.info("Couldn't create dir at %s", path)
        raise liberate.retry(exc=error)

    try:
        lib_status.path = tarname
        lib_status.save()
    except IntegrityError:
        os.rmdir(path)
        raise

    options["path"] = path
    options["tarname"] = tarname

    mail_path = os.path.join(path, 'emails')
    # make maildir
    mailbox.Maildir(mail_path, factory=None)

    inbox_tasks = [liberate_inbox.s(mail_path, inbox.id) for inbox in Inbox.objects.filter(user=user, flags=~Inbox.flags.deleted).only('id').iterator()]
    if len(inbox_tasks) > 0:
        tasks = chord(
                    inbox_tasks,
                    liberate_collect_emails.s(mail_path, options)
                    )
    else:
        options["noEmails"] = True
        data = {"results": []}
        tasks = chain(
                    liberate_fetch_info.s(data, options),
                    liberate_tarball.s(options),
                    liberation_finish.s(options)
                )

    async_result = tasks.apply_async()

    lib_status.async_result = async_result.id
    lib_status.save()


@app.task(rate_limit='100/m')
def liberate_inbox(mail_path, inbox_id):
    """ Gather email IDs """
    inbox = Inbox.objects.get(id=inbox_id, flags=~Inbox.flags.deleted)
    maildir = mailbox.Maildir(mail_path, factory=None)
    maildir.add_folder(str(inbox))

    return {
        'folder': str(inbox),
        'ids': [email.id for email in Email.objects.filter(inbox=inbox, flags=~Email.flags.deleted).only('id').iterator()]
    }


@app.task()
def liberate_collect_emails(results, mail_path, options):
    """ Send off data mining tasks """
    msg_tasks = []
    results = results or []
    for result in results:
        inbox = [(mail_path, result['folder'], email_id) for email_id in result['ids']]
        msg_tasks.extend(inbox)

    task_len = len(msg_tasks)

    if task_len > 0:
        msg_tasks = liberate_message.chunks(msg_tasks, 100).group()
        msg_tasks.skew(step=10)
        msg_tasks = chain(
                        msg_tasks,
                        liberate_convert_box.s(mail_path, options),
                        liberate_fetch_info.s(options),
                        liberate_tarball.s(options),
                        liberation_finish.s(options)
                        )
    else:
        options["noEmails"] = True
        data = {"results": []}
        msg_tasks = chain(
                        liberate_convert_box.s(data, mail_path, options),
                        liberate_fetch_info.s(options),
                        liberate_tarball.s(options),
                        liberation_finish.s(options)
                        )

    async_result = msg_tasks.apply_async()

    lib_status = get_user_model().objects.get(id=options["user"]).liberation
    lib_status.async_result = async_result.id
    lib_status.save()


@app.task(rate_limit='1000/m')
@transaction.atomic()
def liberate_message(mail_path, inbox, email_id):
    """ Take email from database and put on filesystem """
    maildir = mailbox.Maildir(mail_path, factory=None).get_folder(inbox)

    try:
        msg = Email.objects.get(id=email_id, flags=~Email.flags.deleted)
        msg = utils.make_message(msg)
        maildir.add(msg.as_string())
    except Exception, exc:
        msg_id = hex(int(email_id))[2:]
        log.warning("Exception processing %s", msg_id, exc_info=exc)
        return msg_id


@app.task()
def liberate_convert_box(result, mail_path, options):
    """ Convert maildir to mbox if needed """
    if options['storage_type'] == '0':
        pass

    elif options['storage_type'] == '1':
        maildir = mailbox.Maildir(mail_path, factory=None)
        mbox = mailbox.mbox(mail_path + '.mbox')
        mbox.lock()

        for inbox in maildir.list_folders():
            folder = maildir.get_folder(inbox)

            for key in folder.iterkeys():
                msg = folder.pop(key)
                mbox.add(msg)
            maildir.remove_folder(inbox)

        rmtree(mail_path)
        mbox.close()

    return result


@app.task()
def liberate_fetch_info(result, options):
    """Fetch user info and dump json to files"""
    inbox_json = liberate_inbox_metadata(options['user'])
    profile_json = liberate_user_profile(options['user'], result or [])

    with open(os.path.join(options["path"], "profile.json"), "w") as profile:
        profile.write(profile_json)
    with open(os.path.join(options["path"], "inbox.json"), "w") as inbox:
        inbox.write(inbox_json)


@app.task(default_retry_delay=600)
def liberate_tarball(result, options):
    """ Tar up and delete the dir """

    tar_type = TAR_TYPES[options.get('compression_type', '0')]
    tar_name = os.path.join(settings.LIBERATION_PATH, options["tarname"])

    try:
        tar = tarfile.open(tar_name, tar_type['writer'])
    except (IOError, OSError), error:
        log.debug("Couldn't open tarfile at %s", tar_name)
        raise liberate_tarball.retry(exc=error)

    user = get_user_model().objects.get(id=options['user'])
    lib_status = user.liberation

    date = str(lib_status.started)
    dir_name = "inboxen-%s" % date

    try:
        # directories are added recursively by default
        tar.add("%s/" % options["path"], dir_name)
    finally:
        tar.close()
    rmtree(options["path"])

    return tar_name


@app.task(ignore_result=True)
@transaction.atomic()
def liberation_finish(result, options):
    """ Create email to send to user """
    user = get_user_model().objects.get(id=options['user'])
    lib_status = user.liberation
    lib_status.flags.running = False
    lib_status.last_finished = datetime.now(utc)
    lib_status.content_type = int(options.get('compression_type', '0'))

    lib_status.save()

    message = _("Your request for your personal data has been completed. Click <a class=\"alert-link\" href=\"%s\">here</a> to download your archive.")
    message_user(user, safestring.mark_safe(message % urlresolvers.reverse("user-liberate-get")))

    log.info("Finished liberation for %s", options['user'])

    # run a garbage collection on all workers - liberation is leaky
    tasks.force_garbage_collection.delay()


def liberate_user_profile(user_id, email_results):
    """User profile data"""
    data = {
        'preferences': {}
    }
    user = get_user_model().objects.get(id=user_id)

    # user's preferences
    profile = user.userprofile
    data['preferences']['pool_amount'] = profile.pool_amount
    data['preferences']['flags'] = dict(profile.flags.items())

    # user data
    data["id"] = user.id
    data['username'] = user.username
    data["is_active"] = user.is_active
    data['join_date'] = user.date_joined.isoformat()
    data['groups'] = [str(groups) for groups in user.groups.all()]

    data['errors'] = []
    email_results = email_results or []
    for result in email_results:
        if result:
            data['errors'].append(str(result))

    return json.dumps(data)


def liberate_inbox_metadata(user_id):
    """ Grab metadata from inboxes """
    data = {}

    inboxes = Inbox.objects.filter(user__id=user_id)
    for inbox in inboxes:
        address = "%s@%s" % (inbox.inbox, inbox.domain)
        data[address] = {
            "created": inbox.created.isoformat(),
            "flags": dict(inbox.flags.items()),
            "description": inbox.description,
        }

    return json.dumps(data)