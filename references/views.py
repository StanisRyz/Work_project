from django.shortcuts import render


def index(request):
    context = {
        'active_page': 'references',
        'page_title': 'Справочники',
        'page_description': 'Раздел будет использоваться для настройки операций, дефектов, статусов и приоритетов.',
    }
    return render(request, 'references/index.html', context)

# Create your views here.
