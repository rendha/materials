def roles(request):
    """Which links the top bar shows: the Accounts person and the boss do not see the working pages (customer lists, new customer)."""
    user = request.user
    if not user.is_authenticated:
        return {}
    names = set(user.groups.values_list("name", flat=True))
    accounts = user.is_superuser or "Accounts" in names
    boss = user.is_superuser or "Boss" in names
    work = user.is_superuser or "Project" in names or "Store" in names
    return {"is_accounts": accounts, "is_boss": boss, "accounts_only": accounts and not work,
            "no_work_role": not work, "boss_view_only": boss and not work}
