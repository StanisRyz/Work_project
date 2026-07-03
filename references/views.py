from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def index(request):
    context = {
        'active_page': 'references',
        'page_title': 'Справочники',
        'page_description': 'Раздел будет использоваться для настройки операций, дефектов, статусов и приоритетов.',
    }
    return render(request, 'references/index.html', context)
