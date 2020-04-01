# -*- coding: utf-8 -*-

""" License

    Copyright (C) 2014 YUNOHOST.ORG

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program; if not, see http://www.gnu.org/licenses

"""

""" yunohost_permission.py

    Manage permissions
"""

import copy
import grp
import random

from moulinette import m18n
from moulinette.utils.log import getActionLogger
from yunohost.utils.error import YunohostError
from yunohost.user import user_list
from yunohost.log import is_unit_operation

logger = getActionLogger('yunohost.user')

SYSTEM_PERMS = ["mail", "xmpp", "stfp"]

#
#
#  The followings are the methods exposed through the "yunohost user permission" interface
#
#


def user_permission_list(short=False, full=False, ignore_system_perms=False, full_path=True):
    """
    List permissions and corresponding accesses
    """

    # Fetch relevant informations
    from yunohost.app import app_setting, app_list
    from yunohost.utils.ldap import _get_ldap_interface, _ldap_path_extract
    ldap = _get_ldap_interface()
    permissions_infos = ldap.search('ou=permission,dc=yunohost,dc=org',
                                    '(objectclass=permissionYnh)',
                                    ["cn", 'groupPermission', 'inheritPermission',
                                     'URL', 'additionalUrls', 'authHeader', 'label', 'showTile', 'isProtected'])

    # Parse / organize information to be outputed
    app_settings = {app['id']: app_setting(app['id'], 'domain') + app_setting(app['id'], 'path') for app in app_list()['apps']}

    def _complete_url(url, name):
        if url is None:
            return None
        if url.startswith('/'):
            return app_settings[name.split('.')[0]] + url.rstrip("/")
        if url.startswith('re:/'):
            return 're:' + app_settings[name.split('.')[0]] + url.lstrip('re:/')
        else:
            return url

    permissions = {}
    for infos in permissions_infos:

        name = infos['cn'][0]

        if ignore_system_perms and name.split(".")[0] in SYSTEM_PERMS:
            continue

        permissions[name] = {}
        permissions[name]["allowed"] = [_ldap_path_extract(p, "cn") for p in infos.get('groupPermission', [])]

        if full:
            permissions[name]["corresponding_users"] = [_ldap_path_extract(p, "uid") for p in infos.get('inheritPermission', [])]
            permissions[name]["auth_header"] = False if infos.get("authHeader", [False])[0] == "FALSE" else True
            permissions[name]["label"] = infos.get("label", [None])[0]
            permissions[name]["show_tile"] = False if infos.get("showTile", [False])[0] == "FALSE" else True
            permissions[name]["protected"] = False if infos.get("isProtected", [False])[0] == "FALSE" else True
            if full_path and name.split(".")[0] not in SYSTEM_PERMS:
                permissions[name]["url"] = _complete_url(infos.get("URL", [None])[0], name)
                permissions[name]["additional_urls"] = [_complete_url(url, name) for url in infos.get("additionalUrls", [None])]
            else:
                permissions[name]["url"] = infos.get("URL", [None])[0]
                permissions[name]["additional_urls"] = infos.get("additionalUrls", [None])

    if short:
        permissions = permissions.keys()

    return {'permissions': permissions}

@is_unit_operation()
def user_permission_update(operation_logger, permission, add=None, remove=None,
                           label=None, show_tile=None,
                           protected=None, force=False, sync_perm=True):
    """
    Allow or Disallow a user or group to a permission for a specific application

    Keyword argument:
        permission     -- Name of the permission (e.g. mail or or wordpress or wordpress.editors)
        add            -- (optional) List of groups or usernames to add to this permission
        remove         -- (optional) List of groups or usernames to remove from to this permission
        label          -- (optional) Define a name for the permission. This label will be shown on the SSO and in the admin
        show_tile      -- (optional) Define if a tile will be shown in the SSO
        protected      -- (optional) Define if the permission can be added/removed to the visitor group
        force          -- (optional) Give the possibility to add/remove access from the visitor group to a protected permission
    """
    from yunohost.user import user_group_list

    # By default, manipulate main permission
    if "." not in permission:
        permission = permission + ".main"

    existing_permission = user_permission_list(full=True, full_path=False)["permissions"].get(permission, None)

    # Refuse to add "visitors" to mail, xmpp ... they require an account to make sense.
    if add and "visitors" in add and permission.split(".")[0] in SYSTEM_PERMS:
        raise YunohostError('permission_require_account', permission=permission)

    # Refuse to add "visitors" to protected permission
    if ((add and "visitors" in add and existing_permission["protected"]) or \
       (remove and "visitors" in remove and existing_permission["protected"])) and not force:
        raise YunohostError('permission_protected', permission=permission)

    # Fetch currently allowed groups for this permission

    if existing_permission is None:
        raise YunohostError('permission_not_found', permission=permission)

    current_allowed_groups = existing_permission["allowed"]
    operation_logger.related_to.append(('app', permission.split(".")[0]))

    # Compute new allowed group list (and make sure what we're doing make sense)

    new_allowed_groups = copy.copy(current_allowed_groups)
    all_existing_groups = user_group_list()['groups'].keys()

    if add:
        groups_to_add = [add] if not isinstance(add, list) else add
        for group in groups_to_add:
            if group not in all_existing_groups:
                raise YunohostError('group_unknown', group=group)
            if group in current_allowed_groups:
                logger.warning(m18n.n('permission_already_allowed', permission=permission, group=group))
            else:
                operation_logger.related_to.append(('group', group))
                new_allowed_groups += [group]

    if remove:
        groups_to_remove = [remove] if not isinstance(remove, list) else remove
        for group in groups_to_remove:
            if group not in current_allowed_groups:
                logger.warning(m18n.n('permission_already_disallowed', permission=permission, group=group))
            else:
                operation_logger.related_to.append(('group', group))

        new_allowed_groups = [g for g in new_allowed_groups if g not in groups_to_remove]

    # If we end up with something like allowed groups is ["all_users", "volunteers"]
    # we shall warn the users that they should probably choose between one or
    # the other, because the current situation is probably not what they expect
    # / is temporary ?  Note that it's fine to have ["all_users", "visitors"]
    # though, but it's not fine to have ["all_users", "visitors", "volunteers"]
    if "all_users" in new_allowed_groups and len(new_allowed_groups) >= 2:
        if "visitors" not in new_allowed_groups or len(new_allowed_groups) >= 3:
            logger.warning(m18n.n("permission_currently_allowed_for_all_users"))

    # Commit the new allowed group list
    operation_logger.start()

    new_permission = _update_ldap_group_permission(permission=permission, allowed=new_allowed_groups,
                                                   label=label, show_tile=show_tile,
                                                   protected=protected, sync_perm=sync_perm)

    logger.debug(m18n.n('permission_updated', permission=permission))

    return new_permission


@is_unit_operation()
def user_permission_reset(operation_logger, permission, sync_perm=True):
    """
    Reset a given permission to just 'all_users'

    Keyword argument:
        permission -- Name of the permission (e.g. mail or nextcloud or wordpress.editors)
    """

    # By default, manipulate main permission
    if "." not in permission:
        permission = permission + ".main"

    # Fetch existing permission

    existing_permission = user_permission_list(full=True, full_path=False)["permissions"].get(permission, None)
    if existing_permission is None:
        raise YunohostError('permission_not_found', permission=permission)

    if existing_permission["allowed"] == ["all_users"]:
        logger.warning(m18n.n("permission_already_up_to_date"))
        return

    # Update permission with default (all_users)

    operation_logger.related_to.append(('app', permission.split(".")[0]))
    operation_logger.start()

    new_permission = _update_ldap_group_permission(permission=permission, allowed="all_users", sync_perm=sync_perm)

    logger.debug(m18n.n('permission_updated', permission=permission))

    return new_permission

#
#
#  The followings methods are *not* directly exposed.
#  They are used to create/delete the permissions (e.g. during app install/remove)
#  and by some app helpers to possibly add additional permissions
#
#


@is_unit_operation()
def permission_create(operation_logger, permission, allowed=None, 
                      url=None, additional_urls=None, auth_header=True,
                      label=None, show_tile=False, 
                      protected=True, sync_perm=True):
    """
    Create a new permission for a specific application

    Keyword argument:
        permission      -- Name of the permission (e.g. mail or nextcloud or wordpress.editors)
        allowed         -- (optional) List of group/user to allow for the permission
        url             -- (optional) URL for which access will be allowed/forbidden
        additional_urls -- (optional) List of additional URL for which access will be allowed/forbidden
        auth_header     -- (optional) Define for the URL of this permission, if SSOwat pass the authentication header to the application
        label           -- (optional) Define a name for the permission. This label will be shown on the SSO and in the admin. Default is "permission name"
        show_tile       -- (optional) Define if a tile will be shown in the SSO
        protected       -- (optional) Define if the permission can be added/removed to the visitor group

    If provided, 'url' is assumed to be relative to the app domain/path if they
    start with '/'.  For example:
       /                             -> domain.tld/app
       /admin                        -> domain.tld/app/admin
       domain.tld/app/api            -> domain.tld/app/api

    'url' can be later treated as a regex if it starts with "re:".
    For example:
       re:/api/[A-Z]*$               -> domain.tld/app/api/[A-Z]*$
       re:domain.tld/app/api/[A-Z]*$ -> domain.tld/app/api/[A-Z]*$
    """

    from yunohost.utils.ldap import _get_ldap_interface
    from yunohost.user import user_group_list
    ldap = _get_ldap_interface()

    # By default, manipulate main permission
    if "." not in permission:
        permission = permission + ".main"

    # Validate uniqueness of permission in LDAP
    if ldap.get_conflict({'cn': permission},
                         base_dn='ou=permission,dc=yunohost,dc=org'):
        raise YunohostError('permission_already_exist', permission=permission)

    # Get random GID
    all_gid = {x.gr_gid for x in grp.getgrall()}

    uid_guid_found = False
    while not uid_guid_found:
        gid = str(random.randint(200, 99999))
        uid_guid_found = gid not in all_gid

    attr_dict = {
        'objectClass': ['top', 'permissionYnh', 'posixGroup'],
        'cn': str(permission),
        'gidNumber': gid,
        'authHeader': ['TRUE'],
        'label': [str(permission)],
        'showTile': ['FALSE'], # Dummy value, it will be fixed when we call '_update_ldap_group_permission'
        'isProtected': ['FALSE'] # Dummy value, it will be fixed when we call '_update_ldap_group_permission'
    }

    if allowed is not None:
        if not isinstance(allowed, list):
            allowed = [allowed]

    # Validate that the groups to add actually exist
    all_existing_groups = user_group_list()['groups'].keys()
    for group in allowed or []:
        if group not in all_existing_groups:
            raise YunohostError('group_unknown', group=group)

    operation_logger.related_to.append(('app', permission.split(".")[0]))
    operation_logger.start()

    try:
        ldap.add('cn=%s,ou=permission' % permission, attr_dict)
    except Exception as e:
        raise YunohostError('permission_creation_failed', permission=permission, error=e)

    new_permission = _update_ldap_group_permission(permission=permission, allowed=allowed,
                                                   label=label, show_tile=show_tile,
                                                   protected=protected, sync_perm=False)

    permission_url(permission, url=url, add_url=additional_urls, auth_header=auth_header,
                   sync_perm=sync_perm)

    logger.debug(m18n.n('permission_created', permission=permission))
    return new_permission


@is_unit_operation()
def permission_url(operation_logger, permission,
                   url=None, add_url=None, remove_url=None, auth_header=None,
                   clear_urls=False, sync_perm=True):
    """
    Update urls related to a permission for a specific application

    Keyword argument:
        permission  -- Name of the permission (e.g. mail or nextcloud or wordpress.editors)
        url         -- (optional) URL for which access will be allowed/forbidden.
        add_url     -- (optional) List of additional url to add for which access will be allowed/forbidden
        remove_url  -- (optional) List of additional url to remove for which access will be allowed/forbidden
        auth_header -- (optional) Define for the URL of this permission, if SSOwat pass the authentication header to the application
        clear_urls  -- (optional) Clean all urls (url and additional_urls)
    """
    from yunohost.utils.ldap import _get_ldap_interface
    from yunohost.domain import _check_and_normalize_permission_path
    ldap = _get_ldap_interface()

    # By default, manipulate main permission
    if "." not in permission:
        permission = permission + ".main"

    # Fetch existing permission

    existing_permission = user_permission_list(full=True, full_path=False)["permissions"].get(permission, None)
    if not existing_permission:
        raise YunohostError('permission_not_found', permission=permission)

    # TODO -> Check conflict with other app and other URL !!

    if url is None:
        url = existing_permission["url"]
    else:
        url = _check_and_normalize_permission_path(url)

    current_additional_urls = existing_permission["additional_urls"]
    new_additional_urls = copy.copy(current_additional_urls)

    if add_url:
        for ur in add_url:
            if ur in current_additional_urls:
                logger.warning(m18n.n('additional_urls_already_added', permission=permission, url=url))
            else:
                new_additional_urls += [_check_and_normalize_permission_path(url)]

    if remove_url:
        for ur in remove_url:
            if ur not in current_additional_urls:
                logger.warning(m18n.n('additional_urls_already_removed', permission=permission, url=url))

        new_additional_urls = [u for u in new_additional_urls if u not in remove_url]

    if auth_header is None:
        auth_header = existing_permission['auth_header']

    if clear_urls:
        url = None
        new_additional_urls = []

    # Guarantee uniqueness of all values, which would otherwise make ldap.update angry.
    new_additional_urls = set(new_additional_urls)

    # Actually commit the change

    operation_logger.related_to.append(('app', permission.split(".")[0]))
    operation_logger.start()

    try:
        ldap.update('cn=%s,ou=permission' % permission, {'URL': [url] if url is not None else [],
                                                         'additionalUrls': new_additional_urls,
                                                         'authHeader': [str(auth_header).upper()]})
    except Exception as e:
        raise YunohostError('permission_update_failed', permission=permission, error=e)

    if sync_perm:
        permission_sync_to_user()

    logger.debug(m18n.n('permission_updated', permission=permission))
    return user_permission_list(full=True)["permissions"][permission]


@is_unit_operation()
def permission_delete(operation_logger, permission, force=False, sync_perm=True):
    """
    Delete a permission

    Keyword argument:
        permission -- Name of the permission (e.g. mail or nextcloud or wordpress.editors)
    """

    # By default, manipulate main permission
    if "." not in permission:
        permission = permission + ".main"

    if permission.endswith(".main") and not force:
        raise YunohostError('permission_cannot_remove_main')

    from yunohost.utils.ldap import _get_ldap_interface
    ldap = _get_ldap_interface()

    # Make sure this permission exists

    existing_permission = user_permission_list(full=True)["permissions"].get(permission, None)
    if not existing_permission:
        raise YunohostError('permission_not_found', permission=permission)

    # Actually delete the permission

    operation_logger.related_to.append(('app', permission.split(".")[0]))
    operation_logger.start()

    try:
        ldap.remove('cn=%s,ou=permission' % permission)
    except Exception as e:
        raise YunohostError('permission_deletion_failed', permission=permission, error=e)

    if sync_perm:
        permission_sync_to_user()
    logger.debug(m18n.n('permission_deleted', permission=permission))


def permission_sync_to_user():
    """
    Sychronise the inheritPermission attribut in the permission object from the
    user<->group link and the group<->permission link
    """
    import os
    from yunohost.app import app_ssowatconf
    from yunohost.user import user_group_list
    from yunohost.utils.ldap import _get_ldap_interface
    ldap = _get_ldap_interface()

    groups = user_group_list(full=True)["groups"]
    permissions = user_permission_list(full=True, full_path=False)["permissions"]

    for permission_name, permission_infos in permissions.items():

        # These are the users currently allowed because there's an 'inheritPermission' object corresponding to it
        currently_allowed_users = set(permission_infos["corresponding_users"])

        # These are the users that should be allowed because they are member of a group that is allowed for this permission ...
        should_be_allowed_users = set([user for group in permission_infos["allowed"] for user in groups[group]["members"]])

        # Note that a LDAP operation with the same value that is in LDAP crash SLAP.
        # So we need to check before each ldap operation that we really change something in LDAP
        if currently_allowed_users == should_be_allowed_users:
            # We're all good, this permission is already correctly synchronized !
            continue

        new_inherited_perms = {'inheritPermission': ["uid=%s,ou=users,dc=yunohost,dc=org" % u for u in should_be_allowed_users],
                               'memberUid': should_be_allowed_users}

        # Commit the change with the new inherited stuff
        try:
            ldap.update('cn=%s,ou=permission' % permission_name, new_inherited_perms)
        except Exception as e:
            raise YunohostError('permission_update_failed', permission=permission_name, error=e)

    logger.debug("The permission database has been resynchronized")

    app_ssowatconf()

    # Reload unscd, otherwise the group ain't propagated to the LDAP database
    os.system('nscd --invalidate=passwd')
    os.system('nscd --invalidate=group')


def _update_ldap_group_permission(permission, allowed,
                                  label=None, show_tile=None,
                                  protected=None, sync_perm=True):
    """
        Internal function that will rewrite user permission

        permission      -- Name of the permission (e.g. mail or nextcloud or wordpress.editors)
        allowed         -- (optional) A list of group/user to allow for the permission
        label           -- (optional) Define a name for the permission. This label will be shown on the SSO and in the admin
        show_tile       -- (optional) Define if a tile will be shown in the SSO
        protected       -- (optional) Define if the permission can be added/removed to the visitor group


        Assumptions made, that should be checked before calling this function:
        - the permission does currently exists ...
        - the 'allowed' list argument is *different* from the current
          permission state ... otherwise ldap will miserably fail in such
          case...
        - the 'allowed' list contains *existing* groups.
    """

    from yunohost.hook import hook_callback
    from yunohost.utils.ldap import _get_ldap_interface
    ldap = _get_ldap_interface()

    # Fetch currently allowed groups for this permission
    existing_permission = user_permission_list(full=True, full_path=False)["permissions"][permission]

    if allowed is None:
        allowed = existing_permission['allowed']

    if label is None:
        label = existing_permission["label"]

    if show_tile is None:
        show_tile = existing_permission["show_tile"]
    # TODO set show_tile to False if url is regex

    if protected is None:
        protected = existing_permission["protected"]

    # Guarantee uniqueness of all values, which would otherwise make ldap.update angry.
    allowed = set(allowed)

    try:
        ldap.update('cn=%s,ou=permission' % permission,
                    {'groupPermission': ['cn=' + g + ',ou=groups,dc=yunohost,dc=org' for g in allowed],
                     'label': [str(label)] if label != "" else [],
                     'showTile': [str(show_tile).upper()],
                     'isProtected': [str(protected).upper()]
                     })
    except Exception as e:
        raise YunohostError('permission_update_failed', permission=permission, error=e)

    # Trigger permission sync if asked

    if sync_perm:
        permission_sync_to_user()

    new_permission = user_permission_list(full=True)["permissions"][permission]

    # Trigger app callbacks

    app = permission.split(".")[0]
    sub_permission = permission.split(".")[1]

    old_corresponding_users = set(existing_permission["corresponding_users"])
    new_corresponding_users = set(new_permission["corresponding_users"])

    old_allowed_users = set(existing_permission["allowed"])
    new_allowed_users = set(new_permission["allowed"])

    effectively_added_users = new_corresponding_users - old_corresponding_users
    effectively_removed_users = old_corresponding_users - new_corresponding_users

    effectively_added_group = new_allowed_users - old_allowed_users - effectively_added_users
    effectively_removed_group = old_allowed_users - new_allowed_users - effectively_removed_users

    if effectively_added_users or effectively_added_group:
        hook_callback('post_app_addaccess', args=[app, ','.join(effectively_added_users), sub_permission, ','.join(effectively_added_group)])
    if effectively_removed_users or effectively_removed_group:
        hook_callback('post_app_removeaccess', args=[app, ','.join(effectively_removed_users), sub_permission, ','.join(effectively_removed_group)])

    return new_permission
